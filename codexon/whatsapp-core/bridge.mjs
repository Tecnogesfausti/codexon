import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import readline from 'node:readline';
import pino from 'pino';
import QRCode from 'qrcode';
import makeWASocket, {
  Browsers,
  DisconnectReason,
  fetchLatestBaileysVersion,
  getContentType,
  useMultiFileAuthState
} from '@whiskeysockets/baileys';

const DATA_DIR = process.env.CODEXON_WHATSAPP_DATA_DIR || '/data/codexon/whatsapp';
const AUTH_DIR = path.join(DATA_DIR, 'auth');
const CONTACTS_PATH = path.join(DATA_DIR, 'contacts.json');
const MESSAGES_PATH = path.join(DATA_DIR, 'messages.json');
const STATUS_PATH = path.join(DATA_DIR, 'status.json');
const QR_TEXT_PATH = path.join(DATA_DIR, 'qr.txt');
const QR_DATA_URL_PATH = path.join(DATA_DIR, 'qr-data-url.txt');
const REJECT_CALLS = !['0', 'false', 'no', 'off'].includes(
  String(process.env.CODEXON_WHATSAPP_REJECT_CALLS || 'true').toLowerCase()
);
const configuredMessageHistory = Number(
  process.env.CODEXON_WHATSAPP_MESSAGE_HISTORY || 500
);
const MAX_MESSAGE_HISTORY = Number.isFinite(configuredMessageHistory)
  ? Math.max(20, Math.min(5000, configuredMessageHistory))
  : 500;

const logger = pino(
  { level: process.env.CODEXON_WHATSAPP_LOG_LEVEL || 'info' },
  pino.destination(2)
);
const baileysLogger = pino(
  { level: process.env.CODEXON_BAILEYS_LOG_LEVEL || 'silent' },
  pino.destination(2)
);

let socket;
let connectionState = 'starting';
let reconnectTimer;
let shuttingDown = false;
let messageCount = 0;
let lastMessage = null;
let qrText = null;
const contacts = new Map();
const recentMessages = [];
const seenMessageIds = new Set();

fs.mkdirSync(AUTH_DIR, { recursive: true });
loadContacts();
loadMessages();
writeStatus();
startWhatsApp().catch(fatal);

const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
input.on('line', (line) => {
  handleCommand(line).catch((error) => {
    emit({ type: 'send_result', ok: false, error: errorMessage(error) });
  });
});

async function startWhatsApp() {
  clearTimeout(reconnectTimer);
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion();
  socket = makeWASocket({
    version,
    auth: state,
    browser: Browsers.macOS('Codexon'),
    markOnlineOnConnect: true,
    syncFullHistory: false,
    logger: baileysLogger
  });
  socket.ev.on('creds.update', saveCreds);
  socket.ev.on('connection.update', handleConnectionUpdate);
  socket.ev.on('messages.upsert', handleMessagesUpsert);
  socket.ev.on('contacts.upsert', handleContacts);
  socket.ev.on('contacts.update', handleContacts);
  if (REJECT_CALLS) {
    socket.ev.on('call', async (calls) => {
      for (const call of calls) {
        if (call.status === 'offer') {
          await socket.rejectCall(call.id, call.from);
        }
      }
    });
  }
}

async function handleConnectionUpdate(update) {
  const { connection, lastDisconnect, qr } = update;
  if (qr) {
    qrText = qr;
    connectionState = 'qr';
    const dataUrl = await QRCode.toDataURL(qr);
    atomicWrite(QR_TEXT_PATH, `${qr}\n`);
    atomicWrite(QR_DATA_URL_PATH, `${dataUrl}\n`);
    writeStatus();
    emitStatus();
    logger.info('QR de vinculacion generado');
  }
  if (connection === 'open') {
    qrText = null;
    connectionState = 'connected';
    removeIfExists(QR_TEXT_PATH);
    removeIfExists(QR_DATA_URL_PATH);
    writeStatus();
    emitStatus();
    logger.info('sesion WhatsApp conectada');
  }
  if (connection === 'close') {
    const statusCode = lastDisconnect?.error?.output?.statusCode;
    const loggedOut = statusCode === DisconnectReason.loggedOut;
    connectionState = loggedOut ? 'logged_out' : 'disconnected';
    writeStatus({ statusCode });
    emitStatus({ statusCode });
    logger.warn({ statusCode }, 'sesion WhatsApp cerrada');
    if (!loggedOut && !shuttingDown) {
      reconnectTimer = setTimeout(() => startWhatsApp().catch(fatal), 5000);
    }
  }
}

function handleMessagesUpsert({ messages, type }) {
  if (type !== 'notify') return;
  for (const message of messages) {
    if (!message.message || !message.key?.id || seenMessageIds.has(message.key.id)) continue;
    rememberMessageId(message.key.id);
    const normalized = normalizeMessage(message);
    if (!normalized.body) continue;
    lastMessage = normalized;
    messageCount += 1;
    upsertContact({
      id: normalized.from,
      name: normalized.pushName,
      notify: normalized.pushName
    });
    recordMessage({ direction: 'incoming', ...normalized });
    writeStatus();
    emit({ type: 'message', source: 'notify', ...normalized });
  }
}

function normalizeMessage(message) {
  const content = unwrapMessage(message.message);
  const messageType = getContentType(content) || 'unknown';
  return {
    id: message.key.id,
    from: message.key.remoteJid,
    fromMe: Boolean(message.key.fromMe),
    pushName: message.pushName || '',
    timestamp: Number(message.messageTimestamp || Math.floor(Date.now() / 1000)),
    messageType,
    body: extractText(content, messageType)
  };
}

async function handleCommand(line) {
  let command;
  try {
    command = JSON.parse(line);
  } catch {
    throw new Error('comando JSON invalido');
  }
  if (command.type === 'ping') {
    emit({ type: 'pong', id: command.id });
    return;
  }
  if (command.type !== 'send') return;
  if (!socket || connectionState !== 'connected') {
    throw new Error('WhatsApp no esta conectado');
  }
  const jid = normalizeJid(command.to);
  const message = String(command.message || '').trim();
  if (!message) throw new Error('mensaje vacio');
  const sent = await socket.sendMessage(jid, { text: message });
  if (sent?.key?.id) rememberMessageId(sent.key.id);
  upsertContact({ id: jid, name: jidToPhone(jid) || jid });
  recordMessage({
    direction: 'outgoing',
    id: sent?.key?.id || '',
    to: jid,
    fromMe: true,
    timestamp: Math.floor(Date.now() / 1000),
    messageType: 'conversation',
    body: message
  });
  emit({
    type: 'send_result',
    ok: true,
    id: sent?.key?.id || '',
    replyTo: command.replyTo || '',
    to: jid
  });
}

function handleContacts(items) {
  for (const contact of items) upsertContact(contact);
}

function upsertContact(contact) {
  if (!contact?.id || !isContactJid(contact.id)) return;
  const previous = contacts.get(contact.id) || {};
  const displayName =
    preferredContactName(contact) ||
    preferredContactName(previous) ||
    jidToPhone(contact.id) ||
    contact.id;
  contacts.set(contact.id, {
    id: contact.id,
    name: displayName,
    notify: contact.notify || previous.notify || '',
    verifiedName: contact.verifiedName || previous.verifiedName || '',
    phone: jidToPhone(contact.id)
  });
  atomicWrite(
    CONTACTS_PATH,
    `${JSON.stringify({ contacts: [...contacts.values()] }, null, 2)}\n`
  );
}

function preferredContactName(contact) {
  const phone = jidToPhone(contact?.id);
  for (const value of [contact?.name, contact?.notify, contact?.verifiedName]) {
    const candidate = String(value || '').trim();
    if (candidate && candidate !== contact?.id && candidate !== phone) return candidate;
  }
  return '';
}

function loadContacts() {
  try {
    const stored = JSON.parse(fs.readFileSync(CONTACTS_PATH, 'utf8'));
    for (const contact of stored.contacts || []) {
      if (contact?.id) contacts.set(contact.id, contact);
    }
  } catch (error) {
    if (error?.code !== 'ENOENT') logger.warn({ error }, 'no se pudieron cargar contactos');
  }
}

function recordMessage(message) {
  recentMessages.push(message);
  if (recentMessages.length > MAX_MESSAGE_HISTORY) {
    recentMessages.splice(0, recentMessages.length - MAX_MESSAGE_HISTORY);
  }
  atomicWrite(
    MESSAGES_PATH,
    `${JSON.stringify({ messages: recentMessages }, null, 2)}\n`
  );
}

function loadMessages() {
  try {
    const stored = JSON.parse(fs.readFileSync(MESSAGES_PATH, 'utf8'));
    const messages = Array.isArray(stored.messages) ? stored.messages : [];
    recentMessages.push(...messages.slice(-MAX_MESSAGE_HISTORY));
    messageCount = recentMessages.filter(
      (message) => message?.direction === 'incoming'
    ).length;
    lastMessage =
      [...recentMessages].reverse().find(
        (message) => message?.direction === 'incoming'
      ) || null;
  } catch (error) {
    if (error?.code !== 'ENOENT') logger.warn({ error }, 'no se pudieron cargar mensajes');
  }
}

function writeStatus(extra = {}) {
  atomicWrite(
    STATUS_PATH,
    `${JSON.stringify(
      {
        state: connectionState,
        hasQr: Boolean(qrText),
        messageCount,
        lastMessage,
        updatedAt: new Date().toISOString(),
        ...extra
      },
      null,
      2
    )}\n`
  );
}

function emitStatus(extra = {}) {
  emit({
    type: 'status',
    state: connectionState,
    hasQr: Boolean(qrText),
    messageCount,
    ...extra
  });
}

function emit(payload) {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

function atomicWrite(target, value) {
  fs.mkdirSync(path.dirname(target), { recursive: true });
  const temporary = `${target}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, value, { mode: 0o600 });
  fs.renameSync(temporary, target);
}

function removeIfExists(target) {
  try {
    fs.unlinkSync(target);
  } catch (error) {
    if (error?.code !== 'ENOENT') throw error;
  }
}

function rememberMessageId(id) {
  seenMessageIds.add(id);
  if (seenMessageIds.size > 2000) {
    const oldest = seenMessageIds.values().next().value;
    seenMessageIds.delete(oldest);
  }
}

function unwrapMessage(message) {
  if (message?.ephemeralMessage?.message) return unwrapMessage(message.ephemeralMessage.message);
  if (message?.viewOnceMessage?.message) return unwrapMessage(message.viewOnceMessage.message);
  if (message?.viewOnceMessageV2?.message) return unwrapMessage(message.viewOnceMessageV2.message);
  if (message?.documentWithCaptionMessage?.message) {
    return unwrapMessage(message.documentWithCaptionMessage.message);
  }
  return message || {};
}

function extractText(content, messageType) {
  if (messageType === 'conversation') return String(content.conversation || '').trim();
  const payload = content?.[messageType];
  if (!payload || typeof payload !== 'object') return '';
  return String(
    payload.text || payload.caption || payload.extendedTextMessage?.text || ''
  ).trim();
}

function normalizeJid(value) {
  const jid = String(value || '').trim();
  if (jid.endsWith('@s.whatsapp.net') || jid.endsWith('@lid') || jid.endsWith('@g.us')) {
    return jid;
  }
  const digits = jid.replace(/[^\d]/g, '');
  if (!digits) throw new Error('destinatario WhatsApp invalido');
  return `${digits}@s.whatsapp.net`;
}

function isContactJid(jid) {
  return (
    String(jid).endsWith('@s.whatsapp.net') ||
    String(jid).endsWith('@lid') ||
    String(jid).endsWith('@g.us')
  );
}

function jidToPhone(jid) {
  const user = String(jid).split('@')[0] || '';
  return /^\d+$/.test(user) ? user : '';
}

function errorMessage(error) {
  return error instanceof Error ? error.message : String(error);
}

function fatal(error) {
  logger.error({ error }, 'fallo fatal del nucleo WhatsApp');
  process.exitCode = 1;
  setTimeout(() => process.exit(1), 20);
}

async function shutdown() {
  if (shuttingDown) return;
  shuttingDown = true;
  clearTimeout(reconnectTimer);
  connectionState = 'stopped';
  writeStatus();
  emitStatus();
  input.close();
  try {
    socket?.end?.(new Error('Codexon detenido'));
  } catch (error) {
    logger.warn({ error }, 'error cerrando WhatsApp');
  }
  setTimeout(() => process.exit(0), 20);
}

process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
