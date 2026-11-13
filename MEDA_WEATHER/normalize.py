"""Conservative scientific checks, channel mapping and exact SCLK handling."""
from __future__ import annotations
import re
from decimal import Decimal, InvalidOperation

# No global or inferred sentinels. Only verified, fully scoped rules may be added.
SENTINEL_REGISTRY=[]
CONTEXT_FIELDS={"SCLK","LMST","LTST","SOL","SOL_NUMBER"}

def parse_sclk(value):
    s=str(value).strip()
    if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?",s): raise ValueError("SCLK is not a decimal numeric string")
    whole,dot,frac=s.partition(".")
    return {"original":s,"integer_component":whole,"fractional_component":frac if dot else "",
            "original_precision":len(frac) if dot else 0,"normalized":s}

def _finite_number(value):
    try:
        d=Decimal(str(value).strip())
        if not d.is_finite(): return None
        return int(d) if d==d.to_integral_value() else float(d)
    except (InvalidOperation,ValueError,TypeError): return None

def sentinel_rule(product_type,processing_level,source_channel,raw_value):
    for rule in SENTINEL_REGISTRY:
        if (rule.get("verification_status")=="VERIFIED" and rule.get("product_type")==product_type and
            rule.get("processing_level")==processing_level and rule.get("source_channel")==source_channel and
            str(rule.get("exact_value"))==str(raw_value)): return rule
    return None

def validate_channel(field,product_type,processing_level):
    name=field.get("name",""); reasons=[]
    if not name: reasons.append("missing source channel name")
    if not field.get("data_type"): reasons.append("missing XML data_type")
    if not field.get("unit") and name.upper() not in CONTEXT_FIELDS: reasons.append("missing source unit")
    if not field.get("description"): reasons.append("missing scientific definition")
    if reasons: return {"status":"UNVERIFIED","canonical_name":None,"reasons":reasons,"transformation":"none"}
    canonical=re.sub(r"[^a-z0-9]+","_",name.lower()).strip("_")
    return {"status":"VERIFIED","canonical_name":canonical,"reasons":[],"transformation":"identity; source unit and semantics retained"}

def normalize_value(raw,field,product_type,processing_level):
    rule=sentinel_rule(product_type,processing_level,field.get("name"),raw)
    if rule: return None,"VERIFIED_SENTINEL",rule.get("evidence_reference")
    dtype=(field.get("data_type") or "").lower()
    if raw is None or raw=="": return None,"SOURCE_EMPTY",None
    if "real" in dtype or "integer" in dtype:
        val=_finite_number(raw)
        if val is None: return raw,"INVALID_NUMERIC",None
        try:
            numeric=Decimal(str(raw).strip())
            if field.get("minimum") not in (None,"") and numeric < Decimal(field["minimum"]):
                return raw,"BELOW_XML_MINIMUM",{"minimum":field["minimum"]}
            if field.get("maximum") not in (None,"") and numeric > Decimal(field["maximum"]):
                return raw,"ABOVE_XML_MAXIMUM",{"maximum":field["maximum"]}
        except InvalidOperation:
            return raw,"INVALID_XML_RANGE_METADATA",None
        return val,"VALID",None
    return raw,"VALID",None

def normalize_row(row,fields,product_type,processing_level):
    values={}; channels={}; quarantine=[]; byname={f.get("name"):f for f in fields}
    try: sclk=parse_sclk(row.get("SCLK")) if row.get("SCLK") not in (None,"") else None
    except ValueError as e: sclk=None; quarantine.append({"channel":"SCLK","raw_value":row.get("SCLK"),"reason":str(e)})
    for name,raw in row.items():
        if name in CONTEXT_FIELDS: continue
        f=byname.get(name)
        if not f:
            quarantine.append({"channel":name,"raw_value":raw,"reason":"No matching XML field definition","status":"UNVERIFIED"}); continue
        v=validate_channel(f,product_type,processing_level)
        if v["status"]!="VERIFIED":
            quarantine.append({"channel":name,"raw_value":raw,"reason":"; ".join(v["reasons"]),"status":"UNVERIFIED","evidence":f}); continue
        value,status,evidence=normalize_value(raw,f,product_type,processing_level)
        if status in ("INVALID_NUMERIC","VERIFIED_SENTINEL","BELOW_XML_MINIMUM","ABOVE_XML_MAXIMUM","INVALID_XML_RANGE_METADATA"):
            quarantine.append({"channel":name,"raw_value":raw,"reason":status,"status":"QUARANTINED","evidence":evidence}); continue
        values[v["canonical_name"]]=value
        channels[v["canonical_name"]]={"source_channel":name,"unit":f.get("unit"),"scientific_definition":f.get("description"),
            "data_type":f.get("data_type"),"processing_level":processing_level,"xml_evidence":f,
            "canonical_mapping":v["canonical_name"],"transformation":v["transformation"],"validation_status":"VERIFIED"}
    return values,channels,sclk,quarantine
