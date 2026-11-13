"""Command-line orchestration and acceptance decision for MEDA Weather fallback."""
from __future__ import annotations
import argparse, json, logging, os, re, sys, tempfile, shutil, uuid
from collections import Counter, defaultdict
from pathlib import Path
from dataclasses import asdict
from . import __version__
from .parser import inspect_archive, discover_products, iter_csv_rows, sha256_file
from .normalize import normalize_row
from .streaming import SQLiteStage, write_jsonl, file_hash, publish_release, json_safe

LOG=logging.getLogger("meda_weather")
ARTIFACTS=("meda_normalized.jsonl","meda_validation_report.json","meda_quarantine.jsonl","release_manifest.json")

def _product_type(p):
    m=re.search(r"_(CAL|DER)_([A-Z0-9]+)",p.csv_member,re.I)
    return (m.group(2).upper() if m else "UNKNOWN")

def decide_acceptance(archive_safe, accepted_records, failures, quarantined, excluded, accounting_ok):
    """Mandatory gates always apply, including under --no-strict."""
    if not archive_safe or not accounting_ok or accepted_records <= 0:
        return "FAIL"
    if failures or quarantined or excluded:
        return "PARTIAL"
    return "PASS"

def _inside(a,b):
    try: return os.path.commonpath([os.path.realpath(a),os.path.realpath(b)]) in (os.path.realpath(a),os.path.realpath(b))
    except ValueError: return False

def run_pipeline(args):
    run_id=str(uuid.uuid4()); output=Path(args.output).resolve()
    inputs=[Path(args.calibrated).resolve(),Path(args.derived).resolve()]
    if inputs[0]==inputs[1]: raise ValueError("Calibrated and derived archives must be distinct")
    if any(_inside(output,p) or _inside(p,output) for p in inputs): raise ValueError("Output path must not overlap either input archive")
    if any(not p.is_file() for p in inputs): raise FileNotFoundError("Both input ZIP archives must exist")
    report={"run_id":run_id,"package_version":__version__,"acceptance_status":"FAIL","input_archive_inspection":{},
      "archive_hashes":{},"product_discovery":{},"product_details":[],"parsing_results":{},"scientific_validation":{},
      "channel_details":{},"sentinel_rules":{"registry":[],"applied":0},"sclk_metrics":{"seen":0,"invalid":0,"precision_counts":{}},
      "provenance_metrics":{"complete":0,"missing":0},"record_accounting":Counter(),"failures":[],"warnings":[],
      "excluded_data":[],"quarantined_data":0,"publication_decision":"NOT_PUBLISHED","last_successful_stage":"INITIALIZED"}
    archives=[]; products=[]
    for p in inputs:
        ins=inspect_archive(str(p)); report["input_archive_inspection"][p.name]=ins; report["archive_hashes"][p.name]=ins["sha256"]
        if not ins["safe"]: report["failures"].append({"stage":"ARCHIVE_INSPECTION","archive":p.name,"reason":"Unsafe/corrupt/duplicate/unreadable ZIP content"})
        ps=discover_products(str(p),p.name); products.extend((str(p),x) for x in ps); archives.append((p,ins,ps))
    report["product_discovery"]={"total":len(products),"by_archive":{p.name:len(ps) for p,_,ps in archives}}
    report["product_details"]=[asdict(x) for _,x in products]
    report["last_successful_stage"]="PRODUCT_DISCOVERY"
    if args.inspect_only:
        print(json.dumps(report,indent=2,ensure_ascii=False,default=list)); return 0 if not report["failures"] else 2
    temp_parent=str(output.parent); output.parent.mkdir(parents=True,exist_ok=True)
    stage_dir=Path(tempfile.mkdtemp(prefix=output.name+".staging.",dir=temp_parent))
    stage=SQLiteStage(directory=temp_parent,keep=args.keep_temp)
    quarantine_path=stage_dir/"meda_quarantine.jsonl"; quarantine_count=0; quarantined_rows=0
    quarantine_handle=open(quarantine_path,"w",encoding="utf-8",newline="\n")
    def qwrite(item): quarantine_handle.write(json_safe(item)+"\n")
    counts=Counter(); channel_counts=defaultdict(Counter); product_results=[]; record_batch=[]
    try:
        for archive_path,p in products:
            ptype=_product_type(p); pres={"product_id":p.product_id,"archive":p.archive,"csv_member":p.csv_member,
                "status":"PARSED","rows_encountered":0,"rows_parsed":0,"rows_accepted":0,"rows_quarantined":0,"error":None}
            if p.status!="DISCOVERED":
                pres["status"]="UNVERIFIED"; pres["error"]=p.error; report["excluded_data"].append(pres); product_results.append(pres); continue
            fields=p.fields; fieldnames={f.get("name") for f in fields}
            if "SCLK" not in fieldnames:
                pres["status"]="UNVERIFIED"; pres["error"]="XML label has no SCLK field"; report["excluded_data"].append(pres); product_results.append(pres); continue
            try:
                for line,header,row in iter_csv_rows(archive_path,p.csv_member):
                    counts["rows_encountered"]+=1; pres["rows_encountered"]+=1
                    if None in row or set(header)-set(row):
                        counts["rows_rejected"]+=1; pres["rows_quarantined"]+=1; quarantined_rows+=1; quarantine_count+=1
                        qwrite({"source_archive":p.archive,"product_id":p.product_id,"source_file":p.csv_member,
                          "row_reference":line,"raw_row":row,"failure_reason":"CSV row width mismatch","status":"QUARANTINED"})
                        continue
                    counts["rows_parsed"]+=1; pres["rows_parsed"]+=1
                    vals,channels,sclk,q=normalize_row(row,fields,ptype,p.processing_level)
                    report["sclk_metrics"]["seen"]+=1
                    if sclk:
                        prec=str(sclk["original_precision"]); report["sclk_metrics"]["precision_counts"][prec]=report["sclk_metrics"]["precision_counts"].get(prec,0)+1
                    else: report["sclk_metrics"]["invalid"]+=1
                    for name,meta in channels.items(): channel_counts[name]["verified"]+=1
                    if q:
                        quarantined_rows+=1; pres["rows_quarantined"]+=1
                    for item in q:
                        quarantine_count+=1
                        channel_counts[item.get("channel","UNKNOWN")][item.get("status","unverified").lower()]+=1
                        qwrite({"source_archive":p.archive,"product_id":p.product_id,"source_file":p.csv_member,
                          "source_channel":item.get("channel"),"row_reference":line,"sol":p.sol,"original_sclk":row.get("SCLK"),
                          "raw_value":item.get("raw_value"),"failure_reason":item.get("reason"),"evidence":item.get("evidence"),"status":item.get("status","QUARANTINED")})
                    if vals and sclk and p.sol:
                        record={"measurements":vals,"channel_metadata":channels,"source_archive":p.archive,
                          "product_identifier":p.product_id,"source_file":p.csv_member,"source_row":line,
                          "source_channels":list(vals.keys()),"sol":p.sol,"sclk":sclk,"processing_level":p.processing_level,
                          "validation_status":"VERIFIED","quality_information":{"quality_flags":{}}}
                        record_batch.append(record)
                        if len(record_batch)>=1000:
                            stage.insert_many(record_batch); stage.commit(); record_batch.clear()
                        counts["rows_accepted"]+=1; counts["measurements_generated"]+=len(vals)
                        pres["rows_accepted"]+=1; report["provenance_metrics"]["complete"]+=1
                    else:
                        counts["rows_rejected"]+=1
                        if not (sclk and p.sol):
                            report["provenance_metrics"]["missing"]+=1
                            if not q:
                                quarantined_rows+=1; pres["rows_quarantined"]+=1
                            quarantine_count+=1
                            qwrite({"source_archive":p.archive,"product_id":p.product_id,"source_file":p.csv_member,
                              "row_reference":line,"raw_row":row,"failure_reason":"Mandatory SCLK/SOL provenance missing or invalid","status":"QUARANTINED"})
                p.status="PARSED"
            except Exception as e:
                p.status="INVALID"; pres["status"]="FAILED"; pres["error"]=str(e)
                report["failures"].append({"stage":"PRODUCT_PARSE","product_id":p.product_id,"error":str(e)})
            product_results.append(pres)
        if record_batch:
            stage.insert_many(record_batch); record_batch.clear()
        stage.commit()
        report["last_successful_stage"]="PARSING_AND_NORMALIZATION"
        report["channel_details"]={k:dict(v) for k,v in channel_counts.items()}
        report["scientific_validation"]={"verified_channel_names":sorted(channel_counts),"unverified_channels":sum(v.get("unverified",0) for v in channel_counts.values()),
            "note":"Identity mappings only; source XML unit/description retained. No undocumented physical conversions or sentinel substitutions."}
        report["parsing_results"]={"products":product_results,"parsed_products":sum(x["status"]=="PARSED" for x in product_results),
            "failed_products":sum(x["status"]=="FAILED" for x in product_results)}
        # A zero valid-row release is not a meaningful publishable dataset.
        stage.commit(); accepted=counts["rows_accepted"]
        # Acceptance is computed from actual counters and mandatory safety gates.
        archive_safe=all(x["safe"] for x in report["input_archive_inspection"].values())
        accounting_ok=(counts["rows_encountered"] == counts["rows_parsed"]+counts["rows_rejected"])
        status=decide_acceptance(archive_safe,accepted,bool(report["failures"]),quarantine_count,
                                 len(report["excluded_data"]),accounting_ok)
        report["record_accounting"]={**dict(counts),"rows_quarantined":quarantined_rows,"quarantined_measurements_or_events":quarantine_count,
            "rows_published":accepted,"measurements_published":counts["measurements_generated"],
            "rows_encountered_check":counts["rows_parsed"]+counts["rows_rejected"]}
        report["acceptance_status"]=status; report["last_successful_stage"]="ACCEPTANCE_DECISION"
        report["publication_decision"]="PUBLISH_RESTRICTED_RELEASE" if status in ("PASS","PARTIAL") else "DO_NOT_PUBLISH"
        report["quarantined_data"]=quarantine_count
        report["warnings"].append("Quality flag meanings were not inferred; raw quality columns remain subject to conservative channel validation.")
        report["warnings"].append("No UTC conversion, SCLK partition conversion, derived physical interpretation, or undocumented sentinel rule was applied.")
        if status=="FAIL":
            report["failures"].append({"stage":"ACCEPTANCE","reason":"Mandatory acceptance criteria not met"})
        # Partial artifacts are retained separately on FAIL; the live release directory is never touched.
        quarantine_handle.flush(); quarantine_handle.close()
        normalized_path=stage_dir/"meda_normalized.jsonl"
        # Keep accepted partial rows in failure diagnostics too; FAIL never publishes them.
        with open(normalized_path,"w",encoding="utf-8",newline="\n") as f:
            for rec in stage.iter_records(): f.write(json.dumps(rec,ensure_ascii=False,allow_nan=False,separators=(",",":"))+"\n")
        report["last_successful_stage"]="STRUCTURAL_VALIDATION_AND_STAGING"
        report_path=stage_dir/"meda_validation_report.json"
        report["record_accounting"]["measurements_excluded"]=max(0,0)
        report["record_accounting"]["measurements_generated"]=counts["measurements_generated"]
        report["record_accounting"]["measurements_excluded"]=quarantine_count
        report["record_accounting"]["measurements_published"]=counts["measurements_generated"] if status in ("PASS","PARTIAL") else 0
        report["remaining_limitations"]=["Scientific meaning depends on PDS4 label evidence; channels lacking required unit/type/definition are quarantined.",
          "This fallback performs conservative identity normalization, not cross-product joins or temporal aggregation.",
          "Sentinel registry is empty because no channel-specific sentinel rule has been independently established from inspected labels."]
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False,default=list),encoding="utf-8")
        if args.report:
            Path(args.report).resolve().parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(report_path,Path(args.report).resolve())
        if status in ("PASS","PARTIAL"):
            manifest={"run_id":run_id,"package_version":__version__,"acceptance_status":status,
              "input_archive_sha256":report["archive_hashes"],"dataset_sha256":file_hash(normalized_path),
              "quarantine_sha256":file_hash(quarantine_path),"report_sha256":file_hash(report_path),
              "record_counts":report["record_accounting"],"schema_version":"1.0","publication_status":"PUBLISHED"}
            (stage_dir/"release_manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
            if not args.validate_only:
                publish_release(str(stage_dir),str(output),ARTIFACTS); report["publication_decision"]="PUBLISHED"
                LOG.info("Published %s release to %s",status,output)
            else:
                diagnostics=output.with_name(output.name+".validated_"+run_id[:8]); os.replace(stage_dir,diagnostics)
                LOG.info("Validation-only artifacts retained at %s",diagnostics)
        else:
            manifest={"run_id":run_id,"package_version":__version__,"acceptance_status":"FAIL","publication_status":"NOT_PUBLISHED",
                      "input_archive_sha256":report["archive_hashes"],"record_counts":report["record_accounting"]}
            (stage_dir/"release_manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
            faildir=output.with_name(output.name+".failed_"+run_id[:8]); os.replace(stage_dir,faildir)
            LOG.error("Acceptance FAIL; partial diagnostics retained at %s",faildir)
        return 0 if status in ("PASS","PARTIAL") else 3
    except Exception as e:
        report["failures"].append({"stage":"UNHANDLED","error":str(e)}); report["acceptance_status"]="FAIL"
        report["publication_decision"]="DO_NOT_PUBLISH"; report["last_successful_stage"]="EXCEPTION"
        try:
            (stage_dir/"meda_validation_report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2,default=list),encoding="utf-8")
            (stage_dir/"meda_quarantine.jsonl").write_text("",encoding="utf-8")
            (stage_dir/"meda_normalized.jsonl").write_text("",encoding="utf-8")
            (stage_dir/"release_manifest.json").write_text(json.dumps({"run_id":run_id,"acceptance_status":"FAIL","publication_status":"NOT_PUBLISHED"}),encoding="utf-8")
            faildir=output.with_name(output.name+".failed_"+run_id[:8]); os.replace(stage_dir,faildir)
        except Exception: LOG.exception("Could not persist failure diagnostics")
        LOG.exception("Pipeline failed"); return 3
    finally:
        try: quarantine_handle.close()
        except Exception: pass
        stage.close()

def build_parser():
    p=argparse.ArgumentParser(prog="meda-weather",description="MEDA Weather restricted fallback pipeline")
    p.add_argument("--version",action="version",version=__version__)
    sub=p.add_subparsers(dest="command",required=True); r=sub.add_parser("run")
    r.add_argument("--calibrated",required=True); r.add_argument("--derived",required=True); r.add_argument("--output",required=True)
    mode=r.add_mutually_exclusive_group(); mode.add_argument("--strict",action="store_true"); mode.add_argument("--no-strict",action="store_true")
    r.add_argument("--report",default=None); r.add_argument("--inspect-only",action="store_true"); r.add_argument("--validate-only",action="store_true")
    r.add_argument("--keep-temp",action="store_true"); r.add_argument("--log-level",default="INFO",choices=("DEBUG","INFO","WARNING","ERROR"))
    return p

def main(argv=None):
    p=build_parser(); args=p.parse_args(argv); logging.basicConfig(level=getattr(logging,args.log_level),format="%(levelname)s: %(message)s")
    if args.command=="run": return run_pipeline(args)
    return 2

if __name__=="__main__": sys.exit(main())
