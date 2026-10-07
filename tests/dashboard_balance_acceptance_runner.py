"""13 native producer-to-SDK checks; all DB/Sheet data are explicitly synthetic.
The caller must install a fresh temporary DATA_DIR and block external sockets BEFORE importing server.
This harness is not a public endpoint and never sends LINE messages.
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


def run_cases(server, root):
    from dashboard_balance_adapter import adapt_dashboard_data
    from dashboard_flex import compute
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    frozen = datetime.fromisoformat('2026-10-07T00:00:01+08:00')
    def row(k=650,p=42,slot='午餐',sub=False,name='雞胸餐',at=None):
        return dict(k=k,p=p,slot=slot,sub=sub,name=name,at=at)
    cases = [
        (1, [row(700,39.3)], [], 2000,120),
        (2, [], [row(),row(741,45,'晚餐',name='鮭魚餐')],2000,120),
        (3, [row(sub=True)], [row(),row(741,45,'晚餐',name='鮭魚餐')],2000,120),
        (4, [row(sub=True),row(741,45,'晚餐',True,'鮭魚餐')], [row(),row(741,45,'晚餐',name='鮭魚餐')],2000,120),
        (5, [row(2150,102)], [],2000,120),
        (6, [], [row(1100,42),row(1100,40,'晚餐')],2000,120),
        (7, [row(2000,80)], [],2000,120),
        (8, [row(900,50,at='2026-10-06T15:59:59+00:00'), row(10,1,at='2026-10-06T16:00:00+00:00')], [],2000,120),
        (9, [row(10,1,'點心',name='點心'+str(i)) for i in range(8)], [],2000,120),
        (10, [row()], [],2000,120),
        (11, [row(321,20)], [],0,120),
        (12, [], [row(1100,20)],1000,120),
        (13, [], [row(1000,20)],1000,120),
    ]
    out=[]; examples={}
    class Sheet:
        title = 'member-sheet'
        def __init__(self, values): self.values=values
        def get_all_values(self): return self.values
    class Book:
        def __init__(self, values): self.sheet=Sheet(values)
        def worksheet(self,name):
            if name!='member-sheet': raise KeyError(name)
            return self.sheet
    class GC:
        def __init__(self,values): self.book=Book(values)
        def open_by_key(self,key): return self.book
    for number,records,meals,tk,tp in cases:
        uid='U-SYNTHETIC-V53-'+str(number)
        db=root/(str(number)+'.sqlite3')
        byslot={m['slot']:m for m in meals}
        lunch=byslot.get('午餐',{});dinner=byslot.get('晚餐',{})
        values=[['【VIP 客戶檔案】','姓名: 情境測試',f'User_ID: {uid}'],
                ['實際日期','午餐安排','晚餐安排','運動','午餐熱量','午餐蛋白','晚餐熱量','晚餐蛋白','Dispatch_Row_ID'],
                ['2026/10/07',lunch.get('name','無'),dinner.get('name','無'),'無',lunch.get('k',''),lunch.get('p',''),dinner.get('k',''),dinner.get('p',''),'synthetic-dispatch-'+str(number)]]
        with patch.multiple(server, DB_PATH=str(db), DB_DIR=str(root), gc=GC(values), tw_today=lambda:frozen.date(),tw_now=lambda:frozen):
            server.init_db()
            with sqlite3.connect(db) as conn:
                conn.execute('INSERT INTO health_profile(user_id,name,tdee,protein,today_date,sheet_name) VALUES (?,?,?,?,?,?)',(uid,'情境測試',tk,tp,'2026-10-07','member-sheet'))
                for i,r in enumerate(records):
                    key=f'planned-meal:synthetic-dispatch-{number}:{r["slot"]}' if r['sub'] else 'synthetic-'+str(i)
                    server.create_daily_food_log(conn,user_id=uid,product_name=r['name'],meal_slot=r['slot'],consumed_at=r['at'] or frozen.isoformat(),servings=1,nutrition={'calories_kcal':r['k'],'protein_g':r['p']},source_type='planned_meal' if r['sub'] else 'manual',operation_key=key,publish_catalog=False)
                conn.commit()
            data=adapt_dashboard_data(server.get_dashboard_data(uid,scope='home'))
            c=compute(data)
            payload=server.build_dashboard_flex(uid).as_json_dict()
            text=json.dumps(payload,ensure_ascii=False)
            assert '下一餐建議' not in text
            if number==1: assert c['left_k']==1300 and '包月預留' not in text
            if number==2: assert (c['ek'],c['rk'],c['left_k'])==(0,1391,609) and all(not x['eaten'] for x in data['sub_meals'])
            if number==3: assert (c['ek'],c['rk'],c['left_k'])==(650,741,609) and [x['eaten'] for x in data['sub_meals']]==[True,False]
            if number==4: assert (c['ek'],c['rk'],c['left_k'])==(1391,0,609)
            if number==5: assert c['state']=='over' and '已超出' in text and '150' in text and '-150' not in text
            if number==6: assert c['state']=='sub_over' and '包月餐照常吃' in text
            if number==7: assert '熱量已達標，蛋白質還差 40 g' in text
            if number==8: assert c['ek']==10 and len(data['records'])==1  # UTC 16:00 = Taipei midnight
            if number==9: assert '還有 2 筆，請看今日明細' in text
            if number==10: assert '含 AI 估算紀錄' not in text
            if number==11: assert c['state']=='no_target' and '目標未設定' in text and '今日已吃' in text
            if number==12: assert c['hint'].startswith('今天的包月餐合計比目標多') and '吃完包月餐後蛋白質還差' in text and '熱量已達標' not in text
            if number==13: assert '吃完包月餐後熱量剛好達標' in text and '熱量已達標' not in text
            out.append({'case':number,'result':'PASS','state':c['state'],'eaten':c['ek'],'reserved':c['rk'],'balance':c['left_k']})
            name={1:'no_subscription',3:'with_subscription',5:'over'}.get(number)
            if name: examples[name]={'input':data,'message':payload,'computed':c}
    return {'scope':'isolated synthetic DB and fake Sheet; native producer, adapter, renderer and installed LINE SDK; no LINE send','cases':out,'examples':examples}
