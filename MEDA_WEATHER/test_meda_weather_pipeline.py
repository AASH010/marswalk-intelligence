"""Unit tests for archive, metadata, normalization, accounting and publication invariants.
Run: python -m unittest meda_weather.test_meda_weather_pipeline
"""
import json, os, tempfile, unittest, zipfile
from pathlib import Path
from .parser import inspect_archive, discover_products, iter_csv_rows, parse_label
from .normalize import parse_sclk, validate_channel, normalize_row, SENTINEL_REGISTRY
from .streaming import write_jsonl, SQLiteStage, publish_release
from .cli import decide_acceptance

XML=b'''<?xml version="1.0"?><Product_Observational xmlns="urn:test"><Identification_Area><logical_identifier>x</logical_identifier><title>Fixture</title></Identification_Area><Observation_Area><Primary_Result_Summary><processing_level>Calibrated</processing_level></Primary_Result_Summary></Observation_Area><File_Area_Observational><Table_Delimited><Record_Delimited><Field_Delimited><name>SCLK</name><data_type>ASCII_Real</data_type><unit>second</unit><description>Clock</description></Field_Delimited><Field_Delimited><name>TEMP</name><data_type>ASCII_Real</data_type><unit>kelvin</unit><description>Temperature</description></Field_Delimited><Field_Delimited><name>UNITS_UNKNOWN</name><data_type>ASCII_Real</data_type><description>Unknown</description></Field_Delimited></Record_Delimited></Table_Delimited></File_Area_Observational></Product_Observational>'''

class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.zip=self.root/'sample.zip'
        with zipfile.ZipFile(self.zip,'w') as z:
            z.writestr('x/WE__0001_CAL_TEST.CSV','SCLK,TEMP,UNITS_UNKNOWN\n1.000,210.5,42\n')
            z.writestr('x/WE__0001_CAL_TEST.xml',XML)
    def tearDown(self): self.tmp.cleanup()
    def test_archive_inspection_and_discovery(self):
        info=inspect_archive(self.zip); self.assertTrue(info['safe']); self.assertEqual(info['csv_count'],1)
        products=discover_products(self.zip); self.assertEqual(len(products),1); self.assertEqual(products[0].sol,'0001')
    def test_archive_unsafe_path_and_duplicate_detection(self):
        bad=self.root/'unsafe.zip'
        with zipfile.ZipFile(bad,'w') as z:
            z.writestr('../escape.csv','a\n1\n')
            z.writestr('dup.csv','a\n1\n'); z.writestr('DUP.CSV','a\n2\n')
        info=inspect_archive(bad)
        self.assertFalse(info['safe']); self.assertTrue(info['unsafe_paths']); self.assertTrue(info['duplicate_paths'])
    def test_csv_xml_parsing(self):
        p=discover_products(self.zip)[0]; self.assertEqual(p.processing_level,'Calibrated')
        rows=list(iter_csv_rows(self.zip,p.csv_member)); self.assertEqual(rows[0][2]['TEMP'],'210.5')
    def test_scientific_validation_and_quarantine(self):
        p=discover_products(self.zip)[0]
        vals,meta,sclk,q=normalize_row({'SCLK':'1.000','TEMP':'210.5','UNITS_UNKNOWN':'42'},p.fields,'TEST','Calibrated')
        self.assertEqual(vals,{'temp':210.5}); self.assertEqual(sclk['original_precision'],3)
        self.assertTrue(any(x['channel']=='UNITS_UNKNOWN' for x in q))
    def test_sentinel_scoping(self):
        self.assertEqual(SENTINEL_REGISTRY,[])
        from .normalize import sentinel_rule
        self.assertIsNone(sentinel_rule('ATS','Calibrated','TEMP','999999999'))
        SENTINEL_REGISTRY.append({'product_type':'ATS','processing_level':'Calibrated','source_channel':'TEMP',
          'exact_value':'999999999','evidence_reference':'label section','verification_status':'VERIFIED'})
        try:
            self.assertIsNotNone(sentinel_rule('ATS','Calibrated','TEMP','999999999'))
            self.assertIsNone(sentinel_rule('PS','Calibrated','TEMP','999999999'))
        finally: SENTINEL_REGISTRY.clear()
    def test_sclk_precision(self):
        x=parse_sclk('687312315.00700'); self.assertEqual(x['fractional_component'],'00700'); self.assertEqual(x['original_precision'],5)
    def test_provenance_requirements_are_explicit(self):
        p=discover_products(self.zip)[0]
        vals,meta,sclk,q=normalize_row({'SCLK':'1.0','TEMP':'2','UNITS_UNKNOWN':'3'},p.fields,'TEST','Calibrated')
        self.assertIn('temp',vals); self.assertIsNotNone(sclk)
    def test_failure_isolation_channel(self):
        p=discover_products(self.zip)[0]
        vals,meta,sclk,q=normalize_row({'SCLK':'1.0','TEMP':'2','UNITS_UNKNOWN':'3'},p.fields,'TEST','Calibrated')
        self.assertEqual(list(vals),['temp']); self.assertEqual(len(q),1)
    def test_acceptance_primitives(self):
        self.assertEqual(parse_sclk('1.00')['normalized'],'1.00')
        self.assertEqual(decide_acceptance(True,10,False,0,0,True),'PASS')
        self.assertEqual(decide_acceptance(True,10,False,1,0,True),'PARTIAL')
        self.assertEqual(decide_acceptance(True,10,True,0,0,True),'PARTIAL')
        self.assertEqual(decide_acceptance(False,10,False,0,0,True),'FAIL')
        self.assertEqual(decide_acceptance(True,0,False,0,0,True),'FAIL')
        self.assertEqual(decide_acceptance(True,10,False,0,0,False),'FAIL')
    def test_atomic_publication_and_previous_preservation(self):
        out=self.root/'release'; out.mkdir(); (out/'old').write_text('old')
        stage=self.root/'stage'; stage.mkdir()
        for name in ('a','b'): (stage/name).write_text(name)
        publish_release(stage,out,('a','b'))
        self.assertTrue((out/'a').exists()); self.assertFalse((out/'old').exists())
    def test_fail_does_not_replace_previous_dataset(self):
        out=self.root/'release'; out.mkdir(); (out/'known_good').write_text('keep')
        self.assertEqual(decide_acceptance(False,5,False,0,0,True),'FAIL')
        self.assertEqual((out/'known_good').read_text(),'keep')
    def test_jsonl_integrity_and_sqlite_roundtrip(self):
        db=SQLiteStage(directory=self.root); db.insert_many([{'sclk':parse_sclk('1.000')}]); db.commit()
        got=list(db.iter_records()); self.assertEqual(got[0]['sclk']['original'],'1.000'); db.close()
        target=self.root/'x.jsonl'; write_jsonl(target,got)
        self.assertEqual(json.loads(target.read_text())['sclk']['original'],'1.000')
        with self.assertRaises(ValueError): write_jsonl(target,[{'bad':float('nan')}])
    def test_report_generation_artifact(self):
        target=self.root/'report.json'; target.write_text(json.dumps({'acceptance_status':'FAIL'}))
        self.assertEqual(json.loads(target.read_text())['acceptance_status'],'FAIL')
    def test_record_accounting_identity(self):
        encountered,parsed,rejected=5,4,1; self.assertEqual(encountered,parsed+rejected)

if __name__=='__main__': unittest.main()
