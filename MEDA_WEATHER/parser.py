"""Safe PDS4 ZIP discovery, XML metadata extraction and streaming CSV parsing."""
from __future__ import annotations
import csv, hashlib, io, os, re, zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET

@dataclass
class Product:
    archive: str; csv_member: str; xml_member: str | None; product_id: str
    sol: str | None; processing_level: str | None; title: str | None
    fields: list; xml_evidence: dict; status: str = "DISCOVERED"; error: str | None = None

def sha256_file(path, chunk=1024*1024):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for b in iter(lambda:f.read(chunk),b""): h.update(b)
    return h.hexdigest()

def _safe_member(name):
    p=PurePosixPath(name)
    return not p.is_absolute() and ".." not in p.parts and "\\" not in name and not re.match(r"^[A-Za-z]:",name)

def inspect_archive(path):
    r={"path":os.path.abspath(path),"sha256":sha256_file(path),"entries":0,"csv_count":0,"xml_count":0,
       "bad_crc_member":None,"unsafe_paths":[],"duplicate_paths":[],"unreadable_entries":[],"missing_xml":[],"orphan_labels":[]}
    with zipfile.ZipFile(path) as z:
        seen=set(); files={}
        for i in z.infolist():
            if i.is_dir(): continue
            r["entries"]+=1
            if not _safe_member(i.filename): r["unsafe_paths"].append(i.filename)
            if i.filename.casefold() in seen: r["duplicate_paths"].append(i.filename)
            seen.add(i.filename.casefold()); files[i.filename]=i
            ext=PurePosixPath(i.filename).suffix.lower()
            r["csv_count"]+=ext==".csv"; r["xml_count"]+=ext==".xml"
            try:
                with z.open(i) as f:
                    while f.read(1024*1024): pass
            except Exception as e: r["unreadable_entries"].append({"member":i.filename,"error":str(e)})
        try: r["bad_crc_member"]=z.testzip()
        except Exception as e: r["bad_crc_member"]=str(e)
        csvs={str(PurePosixPath(n).with_suffix("")).casefold():n for n in files if n.lower().endswith(".csv")}
        xmls={str(PurePosixPath(n).with_suffix("")).casefold():n for n in files if n.lower().endswith(".xml")}
        r["missing_xml"]=[n for k,n in csvs.items() if k not in xmls]
        r["orphan_labels"]=[n for k,n in xmls.items() if k not in csvs]
    r["safe"]=not any(r[k] for k in ("bad_crc_member","unsafe_paths","duplicate_paths","unreadable_entries"))
    return r

def _local(tag): return tag.rsplit("}",1)[-1]
def _text(el,name):
    for x in el.iter():
        if _local(x.tag)==name and x.text: return x.text.strip()
    return None

def parse_label(data):
    root=ET.fromstring(data); fields=[]
    for e in root.iter():
        if _local(e.tag) not in ("Field_Delimited","Field_Character","Field_Binary"): continue
        d={}
        for c in e:
            k=_local(c.tag)
            if k in ("name","field_number","data_type","unit","description","maximum","minimum","missing_constant"):
                d[k]=(c.text or "").strip()
        if d.get("name"): fields.append(d)
    return {"logical_identifier":_text(root,"logical_identifier"),"title":_text(root,"title"),
      "processing_level":_text(root,"processing_level"),"start_date_time":_text(root,"start_date_time"),
      "stop_date_time":_text(root,"stop_date_time"),"fields":fields}

def discover_products(archive_path, archive_name=None):
    products=[]
    with zipfile.ZipFile(archive_path) as z:
        infos=[i for i in z.infolist() if not i.is_dir()]
        xmlmap={str(PurePosixPath(i.filename).with_suffix("")).casefold():i.filename for i in infos if i.filename.lower().endswith(".xml")}
        for ci in (i for i in infos if i.filename.lower().endswith(".csv")):
            key=str(PurePosixPath(ci.filename).with_suffix("")).casefold(); xm=xmlmap.get(key)
            stem=PurePosixPath(ci.filename).stem; m=re.search(r"WE__(\d+)",stem,re.I)
            try:
                ev=parse_label(z.read(xm)) if xm else {}
                with z.open(ci) as f: line=f.readline().decode("utf-8-sig").strip()
                header=next(csv.reader([line])) if line else []
                products.append(Product(archive_name or os.path.basename(archive_path),ci.filename,xm,
                    ev.get("logical_identifier") or stem,m.group(1) if m else None,ev.get("processing_level"),
                    ev.get("title"),ev.get("fields",[]),ev,"DISCOVERED" if xm and header else "UNVERIFIED",
                    None if xm and header else "Missing XML label or CSV header"))
            except Exception as e:
                products.append(Product(archive_name or os.path.basename(archive_path),ci.filename,xm,stem,
                    m.group(1) if m else None,None,None,[],{},"INVALID",str(e)))
    return products

def iter_csv_rows(archive_path,member,encoding="utf-8-sig"):
    """Yield line number, header and row mapping; never extract archive members."""
    with zipfile.ZipFile(archive_path) as z, z.open(member) as raw:
        text=io.TextIOWrapper(raw,encoding=encoding,newline=""); reader=csv.DictReader(text)
        if not reader.fieldnames: raise ValueError("CSV has no header")
        header=reader.fieldnames
        for line,row in enumerate(reader,start=2): yield line,header,row
