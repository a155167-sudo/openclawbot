import copy
import pytest
from test_gspread_pair_reschedule_adapter import (make_adapter, after_rows, BOOK,OWNER,SOURCE,TARGET, PairSheetConflict,persisted_profile)


def padded_adapter():
    adapter,book,schedule,master=make_adapter()
    # Simulate actual Sheets rectangular readback, including unrelated content AO.
    master.rows.append(['unrelated']+['']*39+['KEEP-AO'])
    original_batch=book.batch_update
    def batch(body):
        # The shared fixture replaces rows; real updateCells preserves trailing cells.
        old=copy.deepcopy(master.rows)
        original_batch(body)
        for req in body['requests']:
            if 'updateCells' in req and req['updateCells']['range']['sheetId']==master.id:
                spec=req['updateCells']['range'];i=spec['startRowIndex'];end=spec['endColumnIndex']
                master.rows[i] += old[i][end:]
    book.batch_update=batch
    master.get_all_values=lambda: [copy.deepcopy(row)+['']*(41-len(row)) for row in master.rows]
    return adapter,book,schedule,master


def test_padded_master_plan_apply_readback_and_reconcile_preserve_other_data():
    a,b,s,m=padded_adapter()
    before=copy.deepcopy(m.rows)
    plan=a.plan_pair_reschedule(workbook_id=BOOK,worksheet_id=101,owner_user_id=OWNER,
        source_date=SOURCE,target_date=TARGET,schedule_rows=after_rows())
    assert b.batch_calls==[]
    assert plan.before_master_view==m.get_all_values()
    assert [r['updateCells']['range']['endColumnIndex'] for r in plan.batch_body['requests']]==[17,17,21,21]
    a.apply_pair_reschedule(plan)
    assert a.read_pair_reschedule(plan)==plan.expected_readback
    assert a.read_pair_rows(owner_user_id=OWNER,source_date=SOURCE,target_date=TARGET)==plan.expected_readback
    assert m.rows[3:]==before[3:]


def plan_for(a, **kw):
    return a.plan_pair_reschedule(workbook_id=BOOK,worksheet_id=101,owner_user_id=OWNER,
        source_date=SOURCE,target_date=TARGET,schedule_rows=after_rows(),**kw)


@pytest.mark.parametrize('index',[0,1,2])
@pytest.mark.parametrize('tail',['DATA',' ',0,False,None,'=FORMULA()'])
def test_real_extra_cells_reject_before_write(index,tail):
    a,b,s,m=padded_adapter()
    m.rows[index].append(tail)
    with pytest.raises(PairSheetConflict):
        plan_for(a)
    assert b.batch_calls==[]


def test_missing_target_append_has_21_cells_and_rectangular_readback():
    a,b,s,m=padded_adapter()
    del m.rows[2]
    before=copy.deepcopy(m.rows)
    plan=plan_for(a,target_master_profile=persisted_profile())
    append=plan.batch_body['requests'][-1]['appendCells']
    assert len(append['rows'][0]['values'])==21
    a.apply_pair_reschedule(plan)
    assert a.read_pair_reschedule(plan)==plan.expected_readback
    assert m.rows[2:-1]==before[2:]


@pytest.mark.parametrize('stage',['pre_apply','post_apply'])
def test_unrelated_tail_drift_is_not_hidden_by_padding(stage):
    a,b,s,m=padded_adapter(); plan=plan_for(a)
    if stage=='post_apply':
        a.apply_pair_reschedule(plan)
    m.rows[-1][-1]='CHANGED-AO'
    with pytest.raises(PairSheetConflict):
        a.apply_pair_reschedule(plan) if stage=='pre_apply' else a.read_pair_reschedule(plan)
    assert len(b.batch_calls)==(1 if stage=='post_apply' else 0)


def test_padded_timeout_readback_reconciles_without_second_write():
    a,b,s,m=padded_adapter();b.outcome='apply_then_timeout'
    plan=plan_for(a)
    with pytest.raises(TimeoutError):
        a.apply_pair_reschedule(plan)
    assert a.read_pair_reschedule(plan)==plan.expected_readback
    assert a.read_pair_rows(owner_user_id=OWNER,source_date=SOURCE,target_date=TARGET)==plan.expected_readback
    assert len(b.batch_calls)==1


def test_padding_does_not_accept_wrong_header():
    a,b,s,m=padded_adapter();m.rows[0][0]='User_ID'
    with pytest.raises(PairSheetConflict): plan_for(a)
    assert b.batch_calls==[]
