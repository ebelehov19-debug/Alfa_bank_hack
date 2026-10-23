import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from news_parser.check import validate
from news_parser.cleanup import CHECK_DIRECTORIES, cleanup
from news_parser.models import Article, SOURCES, news_id
from news_parser.storage import Storage, atomic_json

NOW = datetime(2026,10,23,6,0,tzinfo=timezone.utc)
BODY = 'Текст новости для проверки сохранности исходных данных и дедупликации при обновлении парсера.'


def legacy_batch():
    rows=[]
    for source,url in [('moex','https://moex.com/n1'),('tass','https://tass.ru/ekonomika/1')]:
        rows.append(Article(news_id(source,url),source,url,'Заголовок',BODY,NOW,NOW).to_dict())
    rows.append({**rows[-1],'id':'e_disclosure:'+'a'*24,'source':'e_disclosure',
                 'url':'https://e-disclosure.ru/portal/event.aspx?EventId=OLD'})
    report={'schema_version':1,'count':3,'limit_per_source':200,
            'window_start':(NOW-timedelta(hours=48)).isoformat(),'window_end':NOW.isoformat(),
            'sources':{s:{'status':'ok','exported':1} for s in ['moex','tass','e_disclosure']}}
    files={f'{s}.json':[r for r in rows if r['source']==s] for s in report['sources']}
    files.update({'news.json':rows,'run_report.json':report,
                  'state.json':{'schema_version':1,'seen':{r['id']:r['published_at'] for r in rows}}})
    return files


def test_cleanup_migrates_batch_preserves_data_and_only_removes_known_check_folders(tmp_path):
    old=legacy_batch()
    for name,value in old.items(): atomic_json(tmp_path/name,value)
    for name in CHECK_DIRECTORIES:
        (tmp_path/name).mkdir(); (tmp_path/name/'payload.json').write_text('[]')
    keep=tmp_path/'tass-important'; keep.mkdir(); (keep/'keep.txt').write_text('keep')
    browser={'cookies':[{'domain':'.tass.ru','name':'ok','value':'1'},
                        {'domain':'.e-disclosure.ru','name':'old','value':'2'}],
             'origins':[{'origin':'https://tass.ru','localStorage':[]},
                        {'origin':'https://www.e-disclosure.ru','localStorage':[]}],
             '_news_parser':{'user_agent':'UA'}}
    atomic_json(tmp_path/'.browser-state.json',browser)
    result=cleanup(tmp_path)
    assert set(result['removed'])==set(CHECK_DIRECTORIES)|{'e_disclosure.json'}
    assert result['discarded_state_ids']==1
    assert validate(tmp_path)['count']==2
    assert (keep/'keep.txt').read_text()=='keep'
    for s in SOURCES:
        assert json.loads((tmp_path/f'{s}.json').read_text())==old[f'{s}.json']
    assert set(json.loads((tmp_path/'state.json').read_text())['seen'])=={
        r['id'] for r in old['news.json'] if r['source'] in SOURCES}
    clean_browser=json.loads((tmp_path/'.browser-state.json').read_text())
    assert clean_browser['cookies']==browser['cookies'][:1]
    assert clean_browser['origins']==browser['origins'][:1]
    assert clean_browser['_news_parser']==browser['_news_parser']
    assert cleanup(tmp_path)['removed']==[]


def test_interrupted_legacy_transaction_recovers_to_two_sources(tmp_path):
    files=legacy_batch(); atomic_json(tmp_path/'.pending.json',files)
    atomic_json(tmp_path/'e_disclosure.json',files['e_disclosure.json'])
    with Storage(tmp_path).lock(): pass
    assert validate(tmp_path)['count']==2
    assert not (tmp_path/'.pending.json').exists() and not (tmp_path/'e_disclosure.json').exists()
    assert set(json.loads((tmp_path/'run_report.json').read_text())['sources'])==set(SOURCES)


def test_normal_commit_removes_stale_former_source_file_and_filters_seen_state(tmp_path):
    files=legacy_batch()
    atomic_json(tmp_path/'state.json',files['state.json'])
    atomic_json(tmp_path/'e_disclosure.json',[])
    s=Storage(tmp_path)
    with s.lock():
        state=s.state(NOW-timedelta(hours=48))
        assert set(state)=={r['id'] for r in files['news.json'] if r['source'] in SOURCES}
        report={**files['run_report.json'],'count':0,
                'sources':{k:{'status':'ok','exported':0} for k in SOURCES}}
        s.commit([],state,report)
    assert not (tmp_path/'e_disclosure.json').exists()
    assert validate(tmp_path)['count']==0


def test_cleanup_does_not_follow_debug_directory_symlink(tmp_path):
    data=tmp_path/'data';data.mkdir();outside=tmp_path/'outside';outside.mkdir()
    (outside/'keep').write_text('keep');(data/'tass-check').symlink_to(outside,target_is_directory=True)
    assert cleanup(data)['removed']==['tass-check']
    assert (outside/'keep').read_text()=='keep'


def test_cleanup_refuses_corrupt_state_before_removing_check_directories(tmp_path):
    atomic_json(tmp_path/'state.json',{'schema_version':99,'seen':{}})
    (tmp_path/'tass-check').mkdir()
    with pytest.raises(ValueError): cleanup(tmp_path)
    assert (tmp_path/'tass-check').exists()


def test_cleanup_respects_running_parser_lock(tmp_path):
    with Storage(tmp_path).lock():
        with pytest.raises(RuntimeError):cleanup(tmp_path)


def test_removed_source_is_rejected_by_cli_without_network(monkeypatch):
    import news_parser.__main__ as cli
    monkeypatch.setattr(sys,'argv',['news_parser','--sources','e_disclosure'])
    with pytest.raises(SystemExit) as error:cli.main()
    assert error.value.code==2
