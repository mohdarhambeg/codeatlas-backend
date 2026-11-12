from app.metadata.service import extract_metadata, scan_project


def test_local_python_compatibility(tmp_path):
    source = tmp_path / 'sample.py'
    source.write_text('import math\nclass A:\n    async def m(self, x): return x\ndef f(a): return a\n', encoding='utf-8')
    result = extract_metadata(str(source))
    assert set(result) == {'file', 'imports', 'classes', 'functions'}
    assert result['imports'] == [{'name': 'math', 'line': 1}]
    assert result['classes'][0]['methods'] == [{'name': 'm', 'parameters': ['self', 'x'], 'line': 3}]
    assert result['functions'] == [{'name': 'f', 'parameters': ['a'], 'line': 4}]
    (tmp_path / 'broken.py').write_text('def (')
    ignored = tmp_path / 'node_modules'
    ignored.mkdir()
    (ignored / 'skip.py').write_text('class Skip: pass')
    scan = scan_project(str(tmp_path))
    assert scan['files_scanned'] == 1
    assert scan['files_with_errors'] == 1
    assert scan['summary'] == dict(total_imports=1, total_classes=1, total_functions=1, total_methods=1)


def test_source_is_not_executed(tmp_path):
    source = tmp_path / 'unsafe.py'
    source.write_text('raise RuntimeError("must not execute")\ndef safe(): pass\n')
    assert extract_metadata(str(source))['functions'][0]['name'] == 'safe'
