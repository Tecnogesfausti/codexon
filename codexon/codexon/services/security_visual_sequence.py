"""Bounded, event-specific evidence from Frigate's native snapshot APIs."""
from __future__ import annotations
import asyncio
import hashlib
import math
import os
import re
import time
from pathlib import Path
import httpx
from services.security_visual import utc_now, timestamp, MAX_FRAME_BYTES


def sequence_times(anchor, start, end, now):
    """Allow Frigate's documented <=1 s seek rounding, wholly inside this event."""
    limit=min(end if end is not None else now,now)
    if any(not isinstance(x,(int,float)) or not math.isfinite(x) for x in (anchor,start,limit)):
        return []
    result=[]
    for offset in (-1,1,-2,2,-3,3):
        value=anchor+offset
        if start+1 <= value <= limit-1:
            result.append(value)
        if len(result)==2:break
    return sorted(result)


async def _image(client, path, budget):
    async def fetch():
        async with client.stream('GET',path) as response:
            response.raise_for_status()
            media=response.headers.get('content-type','').split(';')[0]
            if media not in {'image/jpeg','image/png'}:raise ValueError('Invalid image type')
            data=bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data)>MAX_FRAME_BYTES:raise ValueError('Image exceeds limit')
            if len(data)<100 or not (data.startswith(b'\xff\xd8\xff') if media=='image/jpeg' else data.startswith(b'\x89PNG\r\n\x1a\n')):
                raise ValueError('Invalid image bytes')
            return bytes(data),media,timestamp(float(response.headers.get('X-Frame-Time'))) if response.headers.get('X-Frame-Time') else None
    try:
        return await asyncio.wait_for(fetch(),max(.05,budget))
    except (asyncio.TimeoutError,httpx.HTTPError,ValueError):
        return None


def _save(owner,case_id,data,media,**metadata):
    digest=hashlib.sha256(data).hexdigest()
    owner.image_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
    target=owner.image_dir/f'{case_id}-{digest}.{"jpg" if media=="image/jpeg" else "png"}'
    target.write_bytes(data);os.chmod(target,0o600)
    return {'bytes':data,'path':str(target),'sha256':digest,'media_type':media,'received_at':utc_now(),**metadata}


async def capture_sequence(owner,case_id,candidate,config):
    ident=str(candidate.get('event_id') or '')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',ident):raise ValueError('Invalid event ID')
    deadline=time.monotonic()+4.3
    # These calls deliberately carry no Home Assistant or model credential.
    async with httpx.AsyncClient(base_url=config.frigate_base_url,timeout=1.6,follow_redirects=False) as client:
        response=await client.get('/api/events/'+ident);response.raise_for_status();event=response.json()
        camera=event.get('camera');start=timestamp(event.get('start_time'));end=timestamp(event.get('end_time'))
        if event.get('id')!=ident or camera not in config.frigate_cameras or start is None:
            raise ValueError('Event does not match the configured camera')
        snap='/api/events/'+ident+'/snapshot.jpg?timestamp=0&bbox=1&crop=0&quality=90'
        crop='/api/events/'+ident+'/snapshot.jpg?timestamp=0&bbox=0&crop=1&height=640&quality=90'
        original,cropped=await asyncio.gather(_image(client,snap,min(1.6,deadline-time.monotonic())),_image(client,crop,min(1.6,deadline-time.monotonic())))
        if original is None:return []
        data,media,actual=original
        # X-Frame-Time comes from the image actually returned, not from the latest event metadata.
        aligned=actual is not None and start <= actual <= (end if end is not None else time.time())
        common={'event_id':ident,'camera':camera,'event_start':start,'event_end':end}
        anchor=_save(owner,case_id,data,media,source='frigate_event',role='event_anchor',alignment='matched' if aligned else 'unknown',frame_time=actual,timestamp_basis='snapshot_response_header',**common)
        temporal=[]
        targets=sequence_times(actual,start,end,time.time()) if aligned else []
        remaining=deadline-time.monotonic()
        if targets and remaining>.15:
            images=await asyncio.gather(*(_image(client,f'/api/{camera}/recordings/{t:.6f}/snapshot.jpg?height=720',min(1.8,remaining)) for t in targets))
            for t,item in zip(targets,images):
                if item is None:continue
                blob,kind,_=item
                if hashlib.sha256(blob).hexdigest() in {f['sha256'] for f in [anchor,*temporal]}:continue
                temporal.append(_save(owner,case_id,blob,kind,source='frigate_recording',role='temporal_context',alignment='matched',frame_time=t,
                    timestamp_basis='recording_seek',time_uncertainty_seconds=1,anchor_sha256=anchor['sha256'],**common))
        frames=[anchor,*temporal]
        if cropped is not None:
            blob,kind,crop_time=cropped
            if aligned and crop_time==actual and hashlib.sha256(blob).hexdigest()!=anchor['sha256']:
                frames.append(_save(owner,case_id,blob,kind,source='frigate_event_crop',role='detector_crop',alignment='matched',frame_time=crop_time,
                    parent_sha256=anchor['sha256'],timestamp_basis='snapshot_response_header',detail_note='Recorte nativo ampliado; no añade información ni es otro momento.',**common))
        owner.memory.set_setting('security_visual.sequence_status',__import__('json').dumps({'case_id':case_id,'at':utc_now(),'event_frames':1+len(temporal),
            'crop':any(f['source']=='frigate_event_crop' for f in frames),'target_event_frames':3,'capture_ms':int((time.monotonic()-(deadline-4.3))*1000)}))
        return frames


def filter_evidence_valid(frames,event_id,requested_time):
    """Original request anchor stays mandatory; extra evidence must share its provenance."""
    if not frames or any(f.get('alignment')!='matched' or not f.get('sha256') for f in frames):return False
    anchors=[f for f in frames if f.get('source')=='frigate_event' and f.get('frame_time')==requested_time]
    if not anchors:return False
    for frame in frames:
        source=frame.get('source')
        if source=='frigate_event':
            if frame.get('frame_time')!=requested_time:return False
            continue  # Keep existing, strictly matched one-frame reviews compatible.
        valid=False
        for anchor in anchors:
            if anchor.get('event_id')!=event_id or frame.get('event_id')!=event_id or frame.get('camera')!=anchor.get('camera'):
                continue
            t=timestamp(frame.get('frame_time'))
            if t is None:continue
            if source=='frigate_event_crop':
                valid=frame.get('parent_sha256')==anchor['sha256'] and t==requested_time and frame.get('timestamp_basis')=='snapshot_response_header'
            elif source=='frigate_recording':
                begin=timestamp(anchor.get('event_start'));end=timestamp(anchor.get('event_end'))
                uncertainty=frame.get('time_uncertainty_seconds')
                valid=(isinstance(uncertainty,(int,float)) and not isinstance(uncertainty,bool) and 0<=uncertainty<=1 and begin is not None
                    and t-uncertainty>=begin and t+uncertainty<=(end if end is not None else time.time())
                    and abs(t-requested_time)<=3 and frame.get('anchor_sha256')==anchor['sha256'] and frame.get('timestamp_basis')=='recording_seek')
            if valid:break
        if not valid:return False
    return True
