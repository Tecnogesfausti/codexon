const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.listeners = {}; this.style = {}; this.classList = {toggle(){},remove(){}}; }
  setAttribute() {}
  appendChild(e) { this.children.push(e); return e; }
  append(...es) { this.children.push(...es); }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  async click() { for (const f of this.listeners.click || []) await f({preventDefault(){}}); }
  focus() { this.focused = true; }
  select() { this.selected = true; }
  showModal() { this.open = true; }
  close() { for(const f of this.listeners.close || []) f(); }
  remove() { this.removed = true; }
}
async function run() {
  const body = new Element('body'), head = new Element('head'), target = new Element('textarea');
  const events = {}, copied = [], pasted = [], sent = [];
  target.dispatchEvent = e => sent.push(e.data);
  let selection = 'Supervisor error: 401';
  const lines = [{text:'una línea ',isWrapped:false},{text:'larga',isWrapped:true},{text:'',isWrapped:false}];
  const document = {body,head,createElement:t=>new Element(t),getElementById:()=>null,querySelector:()=>target,addEventListener:(t,f)=>(events[t] ||= []).push(f)};
  const navigator = {clipboard:{writeText:async t=>copied.push(t),readText:async()=>'echo prueba'}};
  const term = {getSelection:()=>selection,paste:t=>pasted.push(t),focus(){},buffer:{active:{length:lines.length,getLine:i=>({...lines[i],translateToString:()=>lines[i].text})}}};
  vm.runInNewContext(fs.readFileSync(require('node:path').join(__dirname,'../rootfs/usr/share/codexon-portal/terminal-mobile-keys.js'),'utf8'), {document,navigator,window:{term},requestAnimationFrame(){},InputEvent:class{constructor(t,o){Object.assign(this,o)}}});
  const toolbar = body.children.find(e=>e.id==='codex-mobile-keys');
  const button = name => toolbar.children.find(e=>e.textContent===name);
  await button('Copiar').click();
  assert.deepEqual(copied,[selection]);
  navigator.clipboard.writeText = async()=>{throw new Error('blocked')};
  await button('Copiar').click();
  let dialog = body.children.at(-1), field = dialog.children[2];
  assert.equal(field.value,selection); assert.equal(field.readOnly,true); assert.equal(field.selected,true);
  await dialog.children[3].children[0].click();
  assert.match(dialog.children[1].textContent,/Selecciona/);
  selection=''; await button('Copiar').click();
  assert.match(toolbar.children.at(-1).textContent,/Selecciona/);
  await button('Ver texto').click(); assert.equal(body.children.at(-1).children[2].value,'una línea larga');
  await button('Pegar').click(); dialog=body.children.at(-1);
  assert.deepEqual(pasted,[]); assert.equal(dialog.children[2].value,'echo prueba');
  dialog.children[2].value='echo revisado'; await dialog.children[3].children[0].click();
  assert.deepEqual(pasted,['echo revisado']);
  navigator.clipboard.readText=async()=>{throw new Error('blocked')};
  await button('Pegar').click(); dialog=body.children.at(-1);
  assert.equal(dialog.children[2].value,''); assert.equal(dialog.children[2].readOnly,false);
  dialog.children[2].value='texto móvil'; await dialog.children[3].children[0].click();
  assert.deepEqual(pasted,['echo revisado','texto móvil']);
  await button('Ctrl+C').click(); assert.deepEqual(sent,['\x03']);
  let prevented=false;
  for(const fn of events.keydown) fn({target,ctrlKey:true,shiftKey:false,key:'c',preventDefault(){prevented=true}});
  assert.equal(prevented,false);
  console.log('Terminal clipboard: copy, denied permissions, buffer, reviewed paste, manual paste and Ctrl+C passed.');
}
run().catch(e=>{console.error(e);process.exit(1)});
