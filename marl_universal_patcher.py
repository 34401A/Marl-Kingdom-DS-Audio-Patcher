#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Replace Rhapsody DS audio using local FLAC files and FFmpeg."""
from __future__ import annotations
import os,sys,argparse,hashlib,json,shutil,struct,subprocess,tempfile,time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

HERE=Path(__file__).resolve().parent
MANIFEST=HERE/'source_manifest.json'
OFFICIAL_HASHES={
 '854ceea325407e9c72ddb07d78da5a407e9fe567ff6cbc5df6537be807d32e24':'Japan retail (CM6J)',
 '2803d8ecc8fd22296accc4045e94a67591a17f25bcf82bc233d61a746fd67908':'US retail (CM6E)',
 '0e96a71d2606003618d84881cb7f039fbd3f66ce25433a784fe403771118f1b3':'Europe retail (CM6P)',
}
CODE_REGION={'CM6J':'Japan','CM6E':'US','CM6P':'Europe'}
ALIGN_ROM=0x800

def u16(d,o):return struct.unpack_from('<H',d,o)[0]
def u32(d,o):return struct.unpack_from('<I',d,o)[0]
def be32(d,o):return struct.unpack_from('>I',d,o)[0]
def put32(d,o,v):struct.pack_into('<I',d,o,v)
def beput32(d,o,v):struct.pack_into('>I',d,o,v)
def align_up(x,a):return (x+a-1)//a*a

def sha_file(p):
 h=hashlib.sha256()
 with open(p,'rb') as f:
  for b in iter(lambda:f.read(4<<20),b''):h.update(b)
 return h.hexdigest()

def files_from_nitrofs(d):
 """Read FNT directory tree + FAT by file ID. No fixed file IDs or offsets."""
 if len(d)<0x200:raise ValueError('File is too small to contain a Nintendo DS header')
 fnt,flnt,fat,flat=[u32(d,o) for o in (0x40,0x44,0x48,0x4c)]
 if any(x<0 or x>=len(d) for x in [fnt,fat]) or flnt<8 or flat<8:
  raise ValueError('Invalid NitroFS filesystem header')
 todo=[(0xf000,'')];seen=set();out={}
 while todo:
  did,prefix=todo.pop()
  if did in seen:continue
  seen.add(did)
  head=fnt+8*(did-0xf000)
  if not fnt<=head<=fnt+flnt-8:raise ValueError('FNT directory entry is out of range')
  pos=fnt+u32(d,head);idx=u16(d,head+4)
  while True:
   if not fnt<=pos<fnt+flnt:raise ValueError('FNT entry exceeds directory table')
   c=d[pos];pos+=1
   if c==0:break
   leng=c&0x7f
   if pos+leng>fnt+flnt:raise ValueError('FNT filename exceeds directory table')
   name=bytes(d[pos:pos+leng]).decode('latin1');pos+=leng
   if c&0x80:
    if pos+2>fnt+flnt:raise ValueError('FNT subdirectory ID is out of range')
    todo.append((u16(d,pos),prefix+name+'/'));pos+=2
   else:
    off=fat+8*idx
    if off+8>fat+flat:raise ValueError('FAT entry exceeds allocation table')
    begin,end=struct.unpack_from('<II',d,off)
    if begin>=end or end>len(d):raise ValueError('FAT file extent is outside the ROM: '+prefix+name)
    key=prefix+name
    if key in out:raise ValueError('Duplicate NitroFS path: '+key)
    out[key]={'file_id':idx,'start':begin,'end':end,'fat_entry':off}
    idx+=1
 return out

def crc16(d):
 x=0xffff
 for b in d:
  x^=b
  for _ in range(8):x=(x>>1)^(0xa001 if x&1 else 0)
 return x

def ffmpeg_path():
 for exe in [HERE/'ffmpeg.exe',HERE/'ffmpeg',shutil.which('ffmpeg')]:
  if exe and Path(exe).is_file():return str(exe)
 raise RuntimeError('FFmpeg was not found. Add ffmpeg to PATH or put ffmpeg.exe next to this script.')

# Shift sign convention: negative trims the beginning, positive prepends silence.
# Always convert seconds to integer samples at the *target* sampling rate.
SHIFT_CONFIG=HERE/'audio_shift.json'

def parse_shift_seconds(value,track):
 if isinstance(value,bool):raise ValueError(f'{track}: shift must be a number, not a boolean')
 try:
  v=Decimal(str(value).strip())
 except (InvalidOperation,ValueError):
  raise ValueError(f'{track}: invalid shift value: {value!r}')
 if not v.is_finite() or abs(v)>Decimal('60'):
  raise ValueError(f'{track}: shift must be a finite number between -60 and +60 seconds')
 return v

def track_key(arg,manifest):
 # Accept e.g. song00 / bgm10 / SND/song00.adx / bgm10.adx / WAVE_SE_02.
 key=arg.strip().replace('\\','/')
 if key in manifest:return key
 if key.endswith('.adx') and '/' not in key and ('SND/'+key) in manifest:
  return 'SND/'+key
 if not key.endswith('.adx') and ('SND/'+key+'.adx') in manifest:
  return 'SND/'+key+'.adx'
 if not key.endswith('.adx') and (key+'.adx') in manifest:
  return key+'.adx'
 raise ValueError(f'Unknown track {arg!r}. Use song00, bgm10, WAVE_SE_02, or a NitroFS path.')

def choose_shift_mode(option,has_presets=True):
 """Return whether the configured SONG timing offsets should be applied."""
 if option in ('preset','off'):
  return option=='preset'
 if option!='ask':
  raise ValueError('Invalid shift mode: '+str(option))
 print('Shift: [0] off  [1] preset')
 if not sys.stdin.isatty():
  return False
 while True:
  try:answer=input('Shift [0]: ').strip()
  except EOFError:return False
  if answer in ('','0'):return False
  if answer=='1':return True
  print('Enter 0 or 1.')

def resolve_shifts(manifest,config_file,no_shift,overrides):
 shifts={k:Decimal(0) for k in manifest}
 if not no_shift:
  if not config_file.is_file():raise FileNotFoundError(f'Shift preset file not found: {config_file}')
  obj=json.loads(config_file.read_text(encoding='utf-8-sig'),parse_float=Decimal)
  src=obj.get('shifts_seconds',obj)
  if not isinstance(src,dict):raise ValueError('Shift config must contain a shifts_seconds object')
  for userkey,value in src.items():
   key=track_key(userkey,manifest)
   shifts[key]=parse_shift_seconds(value,key)
 for assignment in overrides:
  if '=' not in assignment:raise ValueError('Use --shift TRACK=SECONDS, for example song00=-0.251 or song26=+0.218')
  userkey,value=assignment.split('=',1)
  key=track_key(userkey,manifest)
  shifts[key]=parse_shift_seconds(value,key)
 for key,seconds in shifts.items():
  v=manifest[key]
  rate=int(v['sample_rate'])
  n=int((seconds*rate).to_integral_value(rounding=ROUND_HALF_UP))
  if n<0 and -n>=int(v['samples']):raise ValueError(f'{key}: requested trim would remove the whole track')
  if n and key=='WAVE_SE_02' and seconds<Decimal('-4.0'):
   raise ValueError('WAVE_SE_02 trim exceeds the supported range')
 return shifts

def samples_for_shift(seconds,rate):
 return int((seconds*int(rate)).to_integral_value(rounding=ROUND_HALF_UP))

def ffmpeg_shift_filter(rate,shift_samples):
 # Resample FIRST to target rate, so fractional-second operations are exact
 # regardless of source format / sample rate or SWAR --wave-rate override.
 filters=[f'aresample={int(rate)}']
 if shift_samples<0:
  filters.extend([f'atrim=start_sample={-shift_samples}','asetpts=PTS-STARTPTS'])
 elif shift_samples>0:
  filters.append(f'adelay={shift_samples}S:all=1')
 return ','.join(filters)

def effective_rate(source_rate,limit):
 """Only downsample, never upsample. None means preserve source rate."""
 source_rate=int(source_rate)
 return min(source_rate,int(limit)) if limit is not None else source_rate

def choose_resample_target(option,selected):
 """Return an optional sample-rate cap. Existing lower rates are preserved."""
 if option=='keep':return None
 if option in ('37800','32768'):return int(option)
 if option!='ask':raise ValueError('Invalid resampling mode: '+str(option))
 print('Sample rate: [0] source  [1] 37800  [2] 32768')
 if not sys.stdin.isatty():
  return None
 while True:
  try:answer=input('Rate [0]: ').strip()
  except EOFError:return None
  if answer in ('','0'):return None
  if answer=='1':return 37800
  if answer=='2':return 32768
  print('Enter 0, 1, or 2.')

def generate_adx(ffmpeg,flac,output,meta,shift_samples=0,output_rate=None):
 """Encode with FFmpeg ADX frames, rewrite a v4 header, 2KiB loop seek alignment."""
 source_rate=int(meta['sample_rate'])
 target_rate=int(output_rate) if output_rate is not None else source_rate
 if not 8000<=target_rate<=96000:raise ValueError('Unsupported ADX output sample rate')
 with tempfile.TemporaryDirectory(prefix='adxframe_') as td:
  base=Path(td)/'encoded.adx'
  cmd=[ffmpeg,'-nostdin','-loglevel','error','-y','-i',str(flac),'-map','0:a:0','-af',ffmpeg_shift_filter(target_rate,shift_samples),'-ar',str(target_rate),'-c:a','adpcm_adx','-f','adx',str(base)]
  p=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
  if p.returncode:raise RuntimeError(f'FFmpeg could not encode {flac.name}: {p.stderr.decode(errors="replace")[-1200:]}')
  d=base.read_bytes()
 if d[:2]!=b'\x80\x00' or d[4:8]!=b'\x03\x12\x04\x02':
  raise ValueError('FFmpeg did not produce the expected stereo ADX format: '+flac.name)
 data_start=int.from_bytes(d[2:4],'big')+4
 if b'(c)CRI' not in d[:data_start]:raise ValueError('ADX CRI header is missing')
 n=be32(d,12);rate=be32(d,8)
 expected_rate=target_rate
 if expected_rate and rate!=expected_rate:raise ValueError('Unexpected ADX sample rate: '+flac.name)
 if not 1<n<=0xffffffff or not 8000<=rate<=96000:raise ValueError('Unsupported ADX encoder parameters')
 frame_count=(n+31)//32
 byte_count=frame_count*36
 if len(d)<data_start+byte_count:raise ValueError('Encoded ADX frame data is incomplete')
 frames=d[data_start:data_start+byte_count]
 # End-marker footer from FFmpeg, reused verbatim.
 footer=d[data_start+byte_count:]
 if not footer.startswith(b'\x80\x01'):raise ValueError('Invalid ADX end marker')
 ls=meta.get('loop_samples')
 if ls:
  # WAV smpl loop samples are indexed in the ORIGINAL source rate.
  # Scale them to the ADX output rate BEFORE applying any time shift.
  st,en=(int((Decimal(int(x))*rate/Decimal(source_rate)).to_integral_value(rounding=ROUND_HALF_UP)) for x in ls)
 elif meta.get('loop_start_seconds_from_original_NDS') is not None:
  st=round(float(meta['loop_start_seconds_from_original_NDS'])*rate);en=n
 else:
  st=en=None
 looping=st is not None
 if looping:
  # Relative markers in the source audio must follow the shifted timeline.
  # For NDS-derived loop-start time, the old code used n as the end marker;
  # that is already the shifted output length and must NOT be shifted twice.
  if ls:
   st+=shift_samples
   en+=shift_samples
  else:
   st+=shift_samples
  if not (0<=st<en<=n+32):raise ValueError(f'Invalid loop range for {flac.name}: {st}..{en} / {n}; the shift may overlap the loop start')
  st=max(0,(st//32)*32)
  header_len=(-((st//32)*36))%2048
  if header_len<64:header_len+=2048
 else:
  header_len=64
 head=bytearray(header_len)
 head[0:2]=b'\x80\x00';struct.pack_into('>H',head,2,header_len-4)
 head[4:8]=d[4:8];head[8:16]=d[8:16];head[16:18]=d[16:18]
 head[18:20]=b'\x04\x00' # CRI ADX v4, essential to game's ADXT streaming library.
 if looping:
  beput32(head,0x20,1);beput32(head,0x24,1)
  start_byte=header_len+st//32*36
  end_byte=header_len+((min(en,n)+31)//32)*36
  beput32(head,0x28,st);beput32(head,0x2c,start_byte)
  beput32(head,0x30,min(en,n));beput32(head,0x34,end_byte)
  if start_byte%2048:raise ValueError('ADX loop offset is not aligned to 2 KiB')
 head[header_len-6:header_len]=b'(c)CRI'
 output.write_bytes(bytes(head)+frames+footer)
 check_adx(output.read_bytes(),expected_rate=rate,expect_loop=looping)
 return {'sample_rate':rate,'source_sample_rate':source_rate,'resampled':rate!=source_rate,'frames':n,'loop':looping,'loop_start_byte':start_byte if looping else None,'bytes':output.stat().st_size,'shift_samples':shift_samples,'shift_seconds_effective':round(shift_samples/rate,8)}

def check_adx(d,expected_rate=None,expect_loop=None):
 if len(d)<72 or d[:2]!=b'\x80\x00' or d[4:8]!=b'\x03\x12\x04\x02' or d[18:20]!=b'\x04\x00':raise ValueError('Unsupported ADX header format')
 size=int.from_bytes(d[2:4],'big')+4
 if size>len(d) or d[size-6:size]!=b'(c)CRI':raise ValueError('Invalid ADX header or CRI marker')
 if expected_rate and be32(d,8)!=expected_rate:raise ValueError('ADX output sample rate mismatch')
 frames=(be32(d,12)+31)//32
 if len(d)<size+frames*36+18:raise ValueError('ADX data is truncated')
 loop=be32(d,0x24)==1
 if expect_loop is not None and expect_loop!=loop:raise ValueError('ADX loop flag mismatch')
 if loop:
  st=be32(d,0x2c);ed=be32(d,0x34)
  if st%2048 or st<size or ed>size+frames*36 or ed<=st:raise ValueError('Invalid ADX loop offsets')
 return True

# DS SWAV ADPCM (IMA variation). step table and index update are hardware-standard.
STEPS=(7,8,9,10,11,12,13,14,16,17,19,21,23,25,28,31,34,37,41,45,50,55,60,66,73,80,88,97,107,118,130,143,157,173,190,209,230,253,279,307,337,371,408,449,494,544,598,658,724,796,876,963,1060,1166,1282,1411,1552,1707,1878,2066,2272,2499,2749,3024,3327,3660,4026,4428,4871,5358,5894,6484,7132,7845,8630,9493,10442,11487,12635,13899,15289,16818,18500,20350,22385,24623,27086,29794,32767)
INDX=(-1,-1,-1,-1,2,4,6,8)
def ima_encode_pcm16(samples):
 from array import array
 if not samples:raise ValueError('Decoded sound effect is empty')
 last=int(samples[0]);idx=0
 packet=bytearray(struct.pack('<hBB',last,0,0));queue=0
 for i,target in enumerate(samples[1:]):
  s=STEPS[idx];diff=int(target)-last
  sign=8 if diff<0 else 0
  if diff<0:diff=-diff
  delta=0;recon=s>>3
  if diff>=s:delta|=4;diff-=s;recon+=s
  if diff>=s>>1:delta|=2;diff-=s>>1;recon+=s>>1
  if diff>=s>>2:delta|=1;recon+=s>>2
  nibble=sign|delta
  last=max(-32768,min(32767,last-recon if sign else last+recon))
  idx=max(0,min(88,idx+INDX[delta]))
  if i&1:packet.append(queue|(nibble<<4))
  else:queue=nibble
 if (len(samples)-1)&1:packet.append(queue)
 while len(packet)%4:packet.append(0)
 return bytes(packet)

def make_swar_wave(ffmpeg,source,rate=44100,shift_samples=0):
 if not 8000<=rate<=48000:raise ValueError('Sound effect sample rate must be between 8,000 and 48,000 Hz')
 command=[ffmpeg,'-nostdin','-loglevel','error','-i',str(source),'-ac','1','-af',ffmpeg_shift_filter(rate,shift_samples),'-ar',str(rate),'-f','s16le','-acodec','pcm_s16le','pipe:1']
 p=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
 if p.returncode:raise RuntimeError('FFmpeg could not decode the SWAR source audio: '+p.stderr.decode(errors='replace')[-350:])
 import array
 smp=array.array('h');smp.frombytes(p.stdout)
 if sys.byteorder!='little':smp.byteswap()
 code=ima_encode_pcm16(smp)
 timer=round(16756991/rate)
 # 12-byte SWAV record + initial IMA predictor/index (4-byte part of sample data), length in words after predictor.
 header=struct.pack('<BBHHHI',2,0,rate,timer,1,(len(code)-4)//4)
 return header+code, len(smp)

def rewrite_sdat_wave02(old,waverecord):
 if old[:4]!=b'SDAT' or u32(old,8)!=len(old):raise ValueError('Unsupported SDAT layout or length')
 fat_off=u32(old,0x20);file_off=u32(old,0x28)
 if old[fat_off:fat_off+4]!=b'FAT ' or old[file_off:file_off+4]!=b'FILE':raise ValueError('Unsupported SDAT FAT/FILE layout')
 if u32(old,fat_off+8)!=3:raise ValueError('SDAT does not contain the expected three internal files; SWAR replacement aborted')
 entry=fat_off+12+2*16;st=u32(old,entry);lng=u32(old,entry+4)
 if old[st:st+4]!=b'SWAR' or st+lng>len(old) or len(old)-(st+lng)>32 or any(old[st+lng:]):raise ValueError('Unexpected padding after SWAR data; replacement aborted')
 sw=bytearray(old[st:st+lng]);
 if u32(sw,8)!=len(sw) or sw[0x10:0x14]!=b'DATA' or u32(sw,0x38)!=15:raise ValueError('Unsupported SWAR structure')
 offs=[u32(sw,0x3c+4*i) for i in range(15)]
 if offs!=sorted(offs) or offs[-1]>=len(sw):raise ValueError('Invalid SWAR sample offset table')
 a,b=offs[2],offs[3]
 if sw[a]!=2 or u16(sw,a+2)!=22050:raise ValueError('WAVE_SE_02 does not match the original 22,050 Hz ADPCM layout; modified SWAR left unchanged')
 delta=len(waverecord)-(b-a)
 new=bytearray(sw[:a]+waverecord+sw[b:]);put32(new,8,len(new));put32(new,0x14,len(new)-16)
 for i in range(3,15):put32(new,0x3c+4*i,offs[i]+delta)
 full=bytearray(old[:st]+new+old[st+lng:])
 put32(full,8,len(full))
 # FILE block size stored at file_off + 4, FAT at entry + 4.
 put32(full,file_off+4,u32(old,file_off+4)+delta)
 put32(full,entry+4,lng+delta)
 if u32(full,file_off+4)!=len(full)-file_off or u32(full,entry+4)!=len(new):raise ValueError('SDAT size verification failed')
 if any(u32(new,0x3c+4*i)>=len(new) for i in range(15)):raise ValueError('SWAR sample offset is out of range')
 return bytes(full)

def classify(romfile,d,paths):
 digest=hashlib.sha256(d).hexdigest();label=OFFICIAL_HASHES.get(digest)
 gamecode=bytes(d[0x0c:0x10]).decode('latin1','replace')
 if label:return label,True,digest
 if gamecode in CODE_REGION:return f'{CODE_REGION[gamecode]} ({gamecode}), modified or unrecognized revision (SHA-256 differs from the retail dump)',False,digest
 return f'Unknown game ID {gamecode!r}; compatible NitroFS and ADX layouts are required',False,digest

def main(argv=None):
 ap=argparse.ArgumentParser(description='Rhapsody DS audio patcher')
 ap.add_argument('rom',nargs='?',help='Path to a .nds ROM (retail or modified)')
 ap.add_argument('-o','--output',help='Custom output ROM path')
 ap.add_argument('--no-swar',action='store_true',help='Replace the 37 ADX tracks only; leave WAVE_SE_02 untouched')
 ap.add_argument('--shift-config',type=Path,default=SHIFT_CONFIG,help='Shift preset JSON file (negative trims the start; positive adds silence)')
 ap.add_argument('--shift-mode',choices=['ask','off','preset'],default='ask',help='Shift preset: ask at launch (default), off, or preset')
 ap.add_argument('--no-shift',action='store_true',help='Disable preset shifts (explicit --shift overrides still apply)')
 ap.add_argument('--shift',action='append',default=[],metavar='TRACK=SECONDS',help='Override a track offset in seconds; may be repeated')
 ap.add_argument('--resample',choices=['ask','keep','37800','32768'],default='ask',help='Pre-encode sample rate: ask at launch (default), keep, 37800, or 32768')
 ap.add_argument('--wave-rate',type=int,default=44100,help='WAVE_SE_02 output sample rate (default 44100; in-game behavior unverified)')
 ap.add_argument('--allow-partial',action='store_true',help='Allow missing ADX tracks to be skipped')
 ap.add_argument('--yes',action='store_true',help='Allow a modified or unrecognized ROM without an interactive confirmation')
 ap.add_argument('--dry-run',action='store_true',help='Validate inputs without patching')
 ap.add_argument('--report',action='store_true',help='Write a detailed JSON report')
 a=ap.parse_args(argv)
 if not a.rom:
  ap.print_help();return 2
 rom=Path(a.rom).expanduser().resolve()
 if not rom.is_file() or rom.suffix.lower()!='.nds':raise ValueError('Please provide an existing .nds ROM file')
 if rom.stat().st_size>1024*1024*1024:raise ValueError('ROM is larger than 1 GiB; refusing to process it')
 manifest=json.loads(MANIFEST.read_text(encoding='utf8'))['audio']; tracks={k:v for k,v in manifest.items() if k.endswith('.adx')}
 d=rom.read_bytes();filenames=files_from_nitrofs(d)
 name,official,digest=classify(rom,d,filenames)
 print(f'{rom.name}: {name}')
 missing=[k for k in tracks if k not in filenames]
 bad=[]
 for k in tracks:
  if k not in filenames:continue
  b=filenames[k];chunk=d[b['start']:b['end']]
  if chunk[:2]!=b'\x80\x00' or chunk[4:8]!=b'\x03\x12\x04\x02' or int.from_bytes(chunk[2:4],'big')+4>=len(chunk):bad.append(k)
 if bad:raise ValueError('The ROM contains unsupported audio tracks: '+', '.join(bad))
 if missing and not a.allow_partial:raise ValueError(f'{len(missing)} expected ADX tracks are missing: {missing}; use --allow-partial to skip them')
 selected={k:v for k,v in tracks.items() if k in filenames}
 if not selected:raise ValueError('No supported MARL audio tracks were found in this ROM')
 rate_limit=choose_resample_target(a.resample,selected)
 converted=[(k,int(v['sample_rate']),effective_rate(v['sample_rate'],rate_limit)) for k,v in selected.items() if effective_rate(v['sample_rate'],rate_limit)!=int(v['sample_rate'])]
 use_presets=False if a.no_shift else choose_shift_mode(a.shift_mode)
 shifts=resolve_shifts(manifest,a.shift_config.expanduser().resolve(),not use_presets,a.shift)

 shift_summary=[(key,value) for key,value in shifts.items() if value and (key in selected or (key=='WAVE_SE_02' and not a.no_swar))]
 print(f'{len(selected)} tracks | rate: {rate_limit or "source"} | shift: {"preset" if use_presets else "off"} | SE_02: {"off" if a.no_swar else "on"}')
 if not official:
  print('Non-retail ROM. Check compatibility before using.')
  if not a.yes and sys.stdin.isatty():
   r=input('Continue? Type YES: ').strip()
   if r!='YES':print('Cancelled');return 3
  elif not a.yes:
   raise RuntimeError('Use --yes when processing a modified ROM without an interactive terminal')
 swar_ok=not a.no_swar and 'SND/sound_data.sdat' in filenames
 if not a.no_swar and not swar_ok:raise ValueError('SND/sound_data.sdat is missing; use --no-swar to skip sound effects')
 files_to_check=list(selected.values())+([manifest['WAVE_SE_02']] if swar_ok else [])
 for i,v in enumerate(files_to_check,1):
  src=(HERE/v['source']).resolve()
  if not src.is_file() or not src.is_relative_to(HERE):raise FileNotFoundError('Source FLAC not found: '+v['source'])
  if sha_file(src)!=v['source_flac_sha256']:raise ValueError('Source FLAC failed SHA-256 verification: '+v['source'])
 if a.dry_run:
  print('OK (dry run)');return 0
 ffmpeg=ffmpeg_path()
 output=Path(a.output).expanduser().resolve() if a.output else rom.with_name(rom.stem+'_HQ_Audio.nds')
 if output==rom:raise ValueError('The output path cannot be the input ROM')
 if output.exists():raise FileExistsError('Output already exists; refusing to overwrite: '+str(output))
 # Start at input file end; ALL original NitroFS regions and binary overlays remain intact.
 # Every new file is independently located in FAT; no reliance on original physical location.
 t0=time.monotonic();log=[]
 print('Patching...')
 try:
  with tempfile.TemporaryDirectory(prefix='marl_audio_') as td:
   td=Path(td)
   shutil.copyfile(rom,output)
   with open(output,'r+b') as writer:
    writer.seek(0,os.SEEK_END)
    for idx,(key,v) in enumerate(selected.items(),1):
     target=td/(key.replace('/','_'));src=HERE/v['source']
     out_rate=effective_rate(v['sample_rate'],rate_limit)
     info=generate_adx(ffmpeg,src,target,v,samples_for_shift(shifts[key],out_rate),output_rate=out_rate)
     off=writer.tell();aoff=align_up(off,ALIGN_ROM)
     writer.write(b'\xff'*(aoff-off));st=writer.tell()
     with target.open('rb') as f:shutil.copyfileobj(f,writer,length=4<<20)
     en=writer.tell()
     writer.seek(filenames[key]['fat_entry']);writer.write(struct.pack('<II',st,en));writer.seek(en)
     log.append({'path':key,'file_id':filenames[key]['file_id'],'rom_start':st,'rom_end':en,**info})
     if idx%5==0 or idx==len(selected):print(f'  ADX {idx}/{len(selected)}',flush=True)
    if swar_ok:
     newwave,smp=make_swar_wave(ffmpeg,HERE/manifest['WAVE_SE_02']['source'],a.wave_rate,samples_for_shift(shifts['WAVE_SE_02'],a.wave_rate))
     original=filenames['SND/sound_data.sdat'];sdat=rewrite_sdat_wave02(d[original['start']:original['end']],newwave)
     off=writer.tell();st=align_up(off,ALIGN_ROM);writer.write(b'\xff'*(st-off));writer.write(sdat);en=writer.tell()
     writer.seek(original['fat_entry']);writer.write(struct.pack('<II',st,en));writer.seek(en)
     log.append({'path':'SND/sound_data.sdat:WAVE_SE_02','file_id':original['file_id'],'rom_start':st,'rom_end':en,'sample_rate':a.wave_rate,'samples':smp,'bytes':len(sdat),'shift_samples':samples_for_shift(shifts['WAVE_SE_02'],a.wave_rate),'shift_seconds_effective':round(samples_for_shift(shifts['WAVE_SE_02'],a.wave_rate)/a.wave_rate,8)})
    # Geometry: DS nominal size power of two between 128KiB and 512MiB. No fake 4GiB FATs.
    used=writer.tell()
    cap=0
    nominal=128*1024
    while nominal<used:
     nominal*=2;cap+=1
    if nominal>512*1024*1024:raise ValueError(f'Output exceeds the 512 MiB NDS ROM capacity limit: {used:,} bytes')
    writer.write(b'\xff'*(nominal-used))
    writer.seek(0x14);writer.write(bytes([cap]));writer.seek(0x80);writer.write(struct.pack('<I',used))
    writer.seek(0);hdr=writer.read(0x15e);writer.seek(0x15e);writer.write(struct.pack('<H',crc16(hdr)))
  # Read-back structural verification: FAT, payload, audio offsets, SDAT.
  newfiles=files_from_nitrofs(output.read_bytes())
  if len(newfiles)!=len(filenames):raise ValueError('NitroFS file count changed unexpectedly')
  with open(output,'rb') as out:
   for x in log:
    key='SND/sound_data.sdat' if ':WAVE_SE_02' in x['path'] else x['path']
    v=newfiles[key];assert v['start']==x['rom_start'] and v['end']==x['rom_end']
    out.seek(v['start']);payload=out.read(v['end']-v['start'])
    if key.endswith('.adx'):check_adx(payload,expect_loop=x['loop'])
    else:assert payload[:4]==b'SDAT' and u32(payload,8)==len(payload)
  if a.report:
   final_log=output.with_suffix('.audio_report.json')
   final_log.write_text(json.dumps({'input':str(rom),'input_sha256':digest,'detected':name,'official':official,'output':str(output),'size_bytes':output.stat().st_size,'elapsed_seconds':round(time.monotonic()-t0,1),'replacements':log,'resample_mode':rate_limit if rate_limit is not None else 'keep','resampled_tracks':len(converted),'shift_preset_enabled':use_presets,'shift_configuration':{k:str(v) for k,v in shifts.items() if v},'notes':'Audio rebuilt from lossless FLAC with optional pre-encode resampling and time shifts; original ROM untouched. SWAR playback requires in-game check.'},ensure_ascii=False,indent=2),encoding='utf8')
  print('Saved:',output)
  return 0
 except BaseException:
  if output.exists():
   try:output.unlink()
   except OSError:pass
  raise

if __name__=='__main__':
 try:sys.exit(main())
 except Exception as exc:
  print('\nError:',exc,file=sys.stderr)
  if os.name=='nt' and len(sys.argv)==2 and sys.stdin.isatty():input('Press Enter to close...')
  sys.exit(1)
