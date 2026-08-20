# -*- coding: utf-8 -*-
"""Build 인천1호선/인천2호선 entries in additional_lines_schedule.json.

- 인천1호선: legacy XLS (BIFF8) 4 sheets, minute-resolution timetable.
- 인천2호선: merged Rail.Blue structured JSON. Official 2026 station name
  `서해구청` is used. Source-blank in-route cells are linearly interpolated
  between the nearest exact timetable anchors; these stops are tagged estimated.

Usage:
  python tools/import_incheon_completion.py \
      --incheon1 '/path/인천1호선 열차운행 시간표.xls' \
      --incheon2 '/path/timetable_db.json'
"""
from __future__ import annotations

import argparse
import json
import math
import struct
from collections import defaultdict
from pathlib import Path
from statistics import median

BASE = Path(__file__).resolve().parents[1]
FREE=0xFFFFFFFF; END=0xFFFFFFFE

# ---------- minimal CFB/BIFF8 reader for the supplied legacy .xls ----------
class CFB:
    def __init__(self, path: Path):
        self.data = path.read_bytes(); h=self.data[:512]
        if h[:8] != bytes.fromhex('D0CF11E0A1B11AE1'):
            raise ValueError('not an OLE/BIFF .xls file')
        self.ss=1<<struct.unpack_from('<H',h,0x1E)[0]
        self.mss=1<<struct.unpack_from('<H',h,0x20)[0]
        self.num_fat=struct.unpack_from('<I',h,0x2C)[0]
        self.first_dir=struct.unpack_from('<I',h,0x30)[0]
        self.cutoff=struct.unpack_from('<I',h,0x38)[0]
        self.first_mini=struct.unpack_from('<I',h,0x3C)[0]
        self.num_mini=struct.unpack_from('<I',h,0x40)[0]
        first_difat=struct.unpack_from('<I',h,0x44)[0]; num_difat=struct.unpack_from('<I',h,0x48)[0]
        difat=[x for x in struct.unpack_from('<109I',h,0x4C) if x not in (FREE,END)]
        sid=first_difat
        for _ in range(num_difat):
            if sid in (END,FREE): break
            vals=list(struct.unpack('<%dI'%(self.ss//4),self.sector(sid)))
            difat += [x for x in vals[:-1] if x not in (FREE,END)]
            sid=vals[-1]
        self.fat=[]
        for fsid in difat[:self.num_fat]:
            self.fat.extend(struct.unpack('<%dI'%(self.ss//4),self.sector(fsid)))
        dbytes=self.chain(self.first_dir)
        self.dir=[]
        for i in range(0,len(dbytes),128):
            e=dbytes[i:i+128]
            if len(e)<128: break
            nlen=struct.unpack_from('<H',e,64)[0]
            name=e[:max(0,nlen-2)].decode('utf-16le','replace') if nlen>=2 else ''
            typ=e[66]; start=struct.unpack_from('<I',e,116)[0]; size=struct.unpack_from('<Q',e,120)[0]
            self.dir.append({'name':name,'type':typ,'start':start,'size':size})
        root=next((e for e in self.dir if e['type']==5),None)
        self.root_stream=self.chain(root['start'])[:root['size']] if root else b''
        self.minifat=[]
        if self.num_mini and self.first_mini not in (END,FREE):
            mb=self.chain(self.first_mini)
            for i in range(0,min(len(mb),self.num_mini*self.ss),4):
                self.minifat.append(struct.unpack_from('<I',mb,i)[0])
    def sector(self,sid):
        o=(sid+1)*self.ss; return self.data[o:o+self.ss]
    def chain(self,start):
        out=bytearray(); sid=start; seen=set()
        while sid not in (END,FREE) and sid < len(self.fat) and sid not in seen:
            seen.add(sid); out += self.sector(sid); sid=self.fat[sid]
        return bytes(out)
    def minichain(self,start,size):
        out=bytearray(); sid=start; seen=set()
        while sid not in (END,FREE) and sid < len(self.minifat) and sid not in seen and len(out)<size:
            seen.add(sid); o=sid*self.mss; out += self.root_stream[o:o+self.mss]; sid=self.minifat[sid]
        return bytes(out[:size])
    def stream(self,name):
        e=next(x for x in self.dir if x['name']==name)
        if e['size'] < self.cutoff and e['type']==2: return self.minichain(e['start'],e['size'])
        return self.chain(e['start'])[:e['size']]

class SSTReader:
    def __init__(self,chunks): self.chunks=chunks; self.ci=0; self.p=0
    def boundary(self): return self.p>=len(self.chunks[self.ci])
    def next_chunk(self): self.ci+=1; self.p=0
    def read(self,n):
        out=bytearray()
        while n:
            if self.boundary(): self.next_chunk()
            take=min(n,len(self.chunks[self.ci])-self.p)
            out += self.chunks[self.ci][self.p:self.p+take]; self.p+=take; n-=take
        return bytes(out)
    def u8(self): return self.read(1)[0]
    def u16(self): return struct.unpack('<H',self.read(2))[0]
    def u32(self): return struct.unpack('<I',self.read(4))[0]
    def chars(self,cch,high):
        out=[]; remaining=cch
        while remaining:
            if self.boundary():
                self.next_chunk(); high=bool(self.u8() & 1)
            bpc=2 if high else 1
            avail=(len(self.chunks[self.ci])-self.p)//bpc
            if avail<=0:
                self.next_chunk(); high=bool(self.u8() & 1); continue
            take=min(remaining,avail); raw=self.read(take*bpc)
            out.append(raw.decode('utf-16le' if high else 'latin1','replace')); remaining-=take
        return ''.join(out)

def _biff_records(book: bytes):
    recs=[]; o=0
    while o+4<=len(book):
        rid,l=struct.unpack_from('<HH',book,o); p=book[o+4:o+4+l]
        if o+4+l>len(book): break
        recs.append((o,rid,p)); o += 4+l
    return recs

def read_xls_sheets(path: Path):
    c=CFB(path)
    name='Workbook' if any(e['name']=='Workbook' for e in c.dir) else 'Book'
    recs=_biff_records(c.stream(name))
    bounds=[]
    for off,r,p in recs:
        if r==0x0085:
            boff=struct.unpack_from('<I',p,0)[0]; cch=p[6]; high=p[7]&1
            nm=p[8:8+cch*(2 if high else 1)].decode('utf-16le' if high else 'latin1','replace')
            bounds.append((nm,boff))
    sst=[]
    for i,(off,r,p) in enumerate(recs):
        if r!=0x00FC: continue
        chunks=[p]; j=i+1
        while j<len(recs) and recs[j][1]==0x003C: chunks.append(recs[j][2]); j+=1
        rd=SSTReader(chunks); rd.u32(); uniq=rd.u32()
        for _ in range(uniq):
            cch=rd.u16(); flags=rd.u8(); high=bool(flags&1); rich=bool(flags&8); ext=bool(flags&4)
            crun=rd.u16() if rich else 0; cbext=rd.u32() if ext else 0
            s=rd.chars(cch,high)
            if crun: rd.read(crun*4)
            if cbext: rd.read(cbext)
            sst.append(s)
        break
    sheets={}
    rec_index={off:i for i,(off,_,_) in enumerate(recs)}
    for nm,boff in bounds:
        cells={}; idx=rec_index[boff]
        for _,r,p in recs[idx+1:]:
            if r==0x000A: break
            if r==0x00FD and len(p)>=10:
                row,col,xf,si=struct.unpack_from('<HHHI',p,0); cells[(row,col)] = sst[si] if si<len(sst) else ''
            elif r==0x0203 and len(p)>=14:
                row,col,xf=struct.unpack_from('<HHH',p,0); cells[(row,col)] = struct.unpack_from('<d',p,6)[0]
        sheets[nm]=cells
    return sheets

# ---------- common helpers ----------
def hm_to_sec(v: str) -> int:
    h,m=[int(x) for x in str(v).strip().split(':')[:2]]
    return h*3600+m*60

def hms_to_sec(v, day_offset=0):
    if not v: return None
    parts=[int(x) for x in str(v).split(':')]
    while len(parts)<3: parts.append(0)
    return int(day_offset or 0)*86400 + parts[0]*3600+parts[1]*60+parts[2]

def monotonic_secs(times):
    out=[]; first=hm_to_sec(times[0]) if times else 0
    # XLS는 영업일 다음날 00~03시를 24시+가 아니라 00시대로 표기한다.
    offset=86400 if first < 4*3600 else 0
    prev=None
    for t in times:
        s=hm_to_sec(t)+offset
        if prev is not None and s < prev-12*3600:
            offset += 86400; s += 86400
        out.append(s); prev=s
    return out

def build_incheon1(path: Path):
    sheets=read_xls_sheets(path)
    expected={'평일_상선','평일_하선','토휴일_상선','토휴일_하선'}
    if set(sheets)!=expected:
        raise ValueError(f'Unexpected sheets: {set(sheets)}')
    day_trains={'weekday':{},'holiday':{}}
    station_reference=None; counts={}
    for sheet_name,cells in sheets.items():
        day='weekday' if sheet_name.startswith('평일') else 'holiday'
        direction='UP' if sheet_name.endswith('상선') else 'DOWN'
        max_col=max(c for r,c in cells if r==0)
        stations=[str(cells.get((0,c),'')).strip() for c in range(1,max_col+1)]
        if not all(stations): raise ValueError(f'blank station header in {sheet_name}')
        if station_reference is None: station_reference=stations
        elif stations != station_reference and stations != list(reversed(station_reference)):
            raise ValueError(f'station headers differ: {sheet_name}')
        max_row=max(r for r,c in cells)
        added=0
        for r in range(1,max_row+1):
            raw_no=cells.get((r,0))
            if raw_no in (None,''): continue
            if isinstance(raw_no,float) and raw_no.is_integer(): train_no=str(int(raw_no))
            else: train_no=str(raw_no).strip()
            nonempty=[]
            for c,st in enumerate(stations,1):
                v=str(cells.get((r,c),'') or '').strip()
                if v: nonempty.append((c,st,v))
            if not nonempty: continue
            secs=monotonic_secs([v for _,_,v in nonempty])
            stops=[]
            for i,((c,st,v),sec) in enumerate(zip(nonempty,secs)):
                stops.append({
                    'station': st,
                    'arr': None if i==0 else sec,
                    'dep': None if i==len(nonempty)-1 else sec,
                    'call': True,
                    'source_resolution_seconds': 60,
                })
            key=train_no
            if key in day_trains[day]: key=f'{train_no}@{direction}'
            day_trains[day][key]={
                'direction':direction,'service':'local','start':stops[0]['station'],'dest':stops[-1]['station'],
                'segment':'인천1호선','source_train_no':train_no,'railblue_train_id':'','stops':stops,
            }
            added+=1
        counts[sheet_name]=added
    return {
        'stations': station_reference,
        'trains': day_trains,
        'segments':['인천1호선'],
        'source_metadata':[{
            'dataset':'인천1호선 열차운행 시간표','source':'user-provided legacy XLS',
            'sheets':counts,'station_count':len(station_reference),
            'time_resolution':'minute','notes':['빈 셀은 해당 운행편 미운행 구간으로 처리','중간 정차역은 분 단위 시각으로 도착/출발 시각을 동일하게 둠'],
        }],
        'capabilities':{'realtime':False,'public_train_no':False,'data_status':'complete'},
    }

def _event_anchor_sec(e):
    vals=[]
    for key,offkey in (('arrival','arrival_day_offset'),('departure','departure_day_offset'),('pass_time','pass_time_day_offset')):
        if e.get(key):
            sec=hms_to_sec(e.get(key), e.get(offkey) or 0)
            if sec is not None: vals.append(sec)
    if not vals: return None
    return sum(vals)/len(vals)

def build_incheon2(path: Path):
    src=json.loads(path.read_text(encoding='utf-8'))
    meta=src['metadata']; raw_trains=src['trains']
    rename=lambda s: '서해구청' if str(s or '').strip()=='서구청' else str(s or '').strip()
    orders={
        'direction_1':[rename(x) for x in meta['full_station_order_updn1']],
        'direction_2':[rename(x) for x in meta['full_station_order_updn2']],
    }
    day_trains={'weekday':{},'holiday':{}}
    interpolation_count=0; exact_count=0; short_turn_count=0
    for tr in raw_trains:
        if str(tr.get('train_type') or '')=='회송': continue
        day='weekday' if tr['service_type']=='weekday' else 'holiday'
        direction_key=tr['direction_key']; order=orders[direction_key]
        origin=rename(tr.get('origin')); dest=rename(tr.get('destination'))
        if origin not in order or dest not in order:
            # Should not happen in merged 27-station dataset.
            continue
        i0=order.index(origin); i1=order.index(dest)
        if i0>i1:
            # source order is already direction-specific, so origin should precede destination.
            continue
        served=order[i0:i1+1]
        if len(served)<len(order): short_turn_count+=1
        events={rename(e.get('station')):e for e in tr.get('events',[]) if e.get('status') in ('stop','pass')}
        anchors={}
        for idx,st in enumerate(served):
            e=events.get(st)
            sec=_event_anchor_sec(e) if e else None
            if sec is not None: anchors[idx]=sec
        # Ensure exact train-level endpoints can anchor interpolation.
        if 0 not in anchors:
            s=hms_to_sec(tr.get('origin_departure'),tr.get('origin_departure_day_offset') or 0)
            if s is not None: anchors[0]=s
        if len(served)-1 not in anchors:
            s=hms_to_sec(tr.get('destination_arrival'),tr.get('destination_arrival_day_offset') or 0)
            if s is not None: anchors[len(served)-1]=s
        if not anchors: continue
        # unwrap anchors monotonically if necessary
        ordered_anchor=list(sorted(anchors.items()))
        prev=None
        for idx,val in ordered_anchor:
            while prev is not None and val < prev-12*3600: val += 86400
            anchors[idx]=val; prev=val
        stops=[]
        for idx,st in enumerate(served):
            e=events.get(st)
            arr=hms_to_sec(e.get('arrival'),e.get('arrival_day_offset') or 0) if e and e.get('arrival') else None
            dep=hms_to_sec(e.get('departure'),e.get('departure_day_offset') or 0) if e and e.get('departure') else None
            pss=hms_to_sec(e.get('pass_time'),e.get('pass_time_day_offset') or 0) if e and e.get('pass_time') else None
            estimated=False
            if arr is None and dep is None and pss is None:
                left=max((j for j in anchors if j<idx),default=None)
                right=min((j for j in anchors if j>idx),default=None)
                if left is not None and right is not None:
                    frac=(idx-left)/(right-left); sec=round(anchors[left]+frac*(anchors[right]-anchors[left]))
                elif left is not None:
                    sec=round(anchors[left]+120*(idx-left))
                elif right is not None:
                    sec=round(anchors[right]-120*(right-idx))
                else: sec=round(next(iter(anchors.values())))
                arr=dep=sec; estimated=True; interpolation_count+=1
            else:
                exact_count+=1
            call = not bool(e and e.get('status')=='pass')
            if pss is not None:
                arr=dep=pss
            if idx==0: arr=None
            if idx==len(served)-1: dep=None
            stops.append({'station':st,'arr':arr,'dep':dep,'call':call,**({'estimated':True} if estimated else {})})
        train_no=str(tr['train_no'])
        day_trains[day][train_no]={
            'direction':'D1' if direction_key=='direction_1' else 'D2','service':'local',
            'start':origin,'dest':dest,'segment':'인천2호선','source_train_no':train_no,
            'railblue_train_id':str(tr.get('railblue_train_id') or ''),'stops':stops,
        }
    stations=orders['direction_1']
    return {
        'stations':stations,
        'trains':day_trains,
        'segments':['인천2호선'],
        'source_metadata':[{
            'dataset':'인천2호선 merged full 27 stations','source':'user-provided Rail.Blue merged structured dataset',
            'weekday_date':'2026-08-20','weekend_holiday_date':'2026-08-22',
            'train_count':sum(len(x) for x in day_trains.values()),'station_count':len(stations),
            'interpolated_stop_count':interpolation_count,'exact_or_source_stop_count':exact_count,
            'short_turn_count':short_turn_count,
            'station_name_correction':'서구청 → 서해구청 (official 2026 station name)',
            'notes':['원본 source_blank/not_captured 중 운행구간 내부의 빈 역 시각은 양쪽 정확 시각 anchor 사이를 선형 보간','보간된 stop에는 estimated=true 저장'],
        }],
        'capabilities':{'realtime':False,'public_train_no':False,'data_status':'complete_with_interpolation'},
    }

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--incheon1',required=True); ap.add_argument('--incheon2',required=True)
    args=ap.parse_args()
    out_path=BASE/'additional_lines_schedule.json'
    data=json.loads(out_path.read_text(encoding='utf-8'))
    data['meta']['version']='V14.2-additional-lines-20260820'
    notes=data['meta'].setdefault('notes',[])
    notes=[x for x in notes if '인천' not in str(x)]
    notes += [
        '인천1호선/인천2호선 are schedule-only; train numbers are internal timetable identifiers and hidden from users',
        '인천2호선 official station name is 서해구청 (I210)',
    ]
    data['meta']['notes']=notes
    data['lines']['인천1호선']=build_incheon1(Path(args.incheon1))
    data['lines']['인천2호선']=build_incheon2(Path(args.incheon2))
    out_path.write_text(json.dumps(data,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    audit={
        'version':'V14.2.0',
        'incheon1':{
            'stations':len(data['lines']['인천1호선']['stations']),
            'weekday_trains':len(data['lines']['인천1호선']['trains']['weekday']),
            'holiday_trains':len(data['lines']['인천1호선']['trains']['holiday']),
            'first_station':data['lines']['인천1호선']['stations'][0],
            'last_station':data['lines']['인천1호선']['stations'][-1],
        },
        'incheon2':{
            'stations':len(data['lines']['인천2호선']['stations']),
            'weekday_trains':len(data['lines']['인천2호선']['trains']['weekday']),
            'holiday_trains':len(data['lines']['인천2호선']['trains']['holiday']),
            'contains_seohaegu_office':'서해구청' in data['lines']['인천2호선']['stations'],
            'contains_old_name':'서구청' in data['lines']['인천2호선']['stations'],
            'interpolated_stop_count':data['lines']['인천2호선']['source_metadata'][0]['interpolated_stop_count'],
        },
    }
    (BASE/'V14_2_INCHEON_IMPORT_AUDIT.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(audit,ensure_ascii=False,indent=2))

if __name__=='__main__': main()
