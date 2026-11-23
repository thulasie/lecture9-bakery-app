"""Checks only temporary copies; the real bakery.db is never modified."""
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
import app

class BakeryTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.original=app.DB
        app.DB=Path(self.folder.name)/'bakery.db'
        if self.original.exists():
            source=sqlite3.connect(self.original)
            dest=sqlite3.connect(app.DB)
            source.backup(dest)
            source.close(); dest.close()
        # Establish a pristine fixture on the temporary copy only.
        with app.connect() as c:
            c.executescript('DROP TABLE IF EXISTS settings; DROP TABLE IF EXISTS orders; DROP TABLE IF EXISTS requests; DROP TABLE IF EXISTS helpers; DROP TABLE IF EXISTS audit;')
        app.initialize()
    def tearDown(self):
        app.DB=self.original
        self.folder.cleanup()
    def action(self,**p):
        with app.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            app.mutate(c,p)
    def next_request(self):
        with app.connect() as c:
            return c.execute("SELECT * FROM requests WHERE status='pending' AND received<=? ORDER BY received,id LIMIT 1",(app.current(c),)).fetchone()
    def decide(self,decision='accepted'):
        self.action(action='decide',id=self.next_request()['id'],decision=decision)
    def test_imports_and_restart(self):
        app.initialize()
        with app.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM orders').fetchone()[0],13)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM requests').fetchone()[0],25)
            self.assertEqual(len(app.state(c)['requests']),3)
            self.assertEqual(app.used(c,'2026-11-26'),6)
    def test_notice_order_and_advance(self):
        with self.assertRaises(ValueError): self.action(action='advance')
        with self.assertRaises(ValueError): self.action(action='decide',id=3,decision='accepted')
        self.decide()
        with self.assertRaises(ValueError): self.decide()
        self.assertEqual(self.next_request()['customer'],'Okafor')
        self.decide('rejected');self.decide()
        self.action(action='advance')
        with app.connect() as c:
            self.assertEqual(app.current(c),'2026-11-24')
    def test_helper_and_capacity(self):
        with self.assertRaises(ValueError): self.action(action='helper',day='2026-11-24')
        self.action(action='helper',day='2026-11-25')
        with self.assertRaises(ValueError): self.action(action='helper',day='2026-11-25')
        with app.connect() as c:
            d=next(d for d in app.state(c)['days'] if d['day']=='2026-11-25')
            self.assertEqual(d['capacity'],12)
            self.assertEqual(d['net'],60)
            r=dict(received='2026-11-23',pickup='2026-11-25',customer='Test',action='new',product='cake',qty=6)
            self.assertFalse(app.assess(c,r)[0])
            r['qty']=5
            self.assertTrue(app.assess(c,r)[0])
    def test_full_week(self):
        # Accept feasible requests; book helpers when a request needs one.
        while True:
            r=self.next_request()
            if r:
                with app.connect() as c: ok,reason,_=app.assess(c,r)
                if 'Book a $100 helper' in reason:
                    self.action(action='helper',day=r['pickup'])
                    ok=True
                self.decide('accepted' if ok else 'rejected')
            else:
                with app.connect() as c: today=app.current(c)
                if today=='2026-12-01':break
                self.action(action='advance')
        with app.connect() as c:
            rs={r['customer']+' '+r['action']:r for r in c.execute('SELECT * FROM requests')}
            for key in ['Okafor new','Lam change','Adams new','Zhou change','Lee cancel']:
                self.assertEqual(rs[key]['status'],'rejected',key)
            for key in ['Brown cancel','Moreau change','Costa cancel','Gupta new','Ito new','Nair new']:
                self.assertEqual(rs[key]['status'],'accepted',key)
            self.assertEqual(app.used(c,'2026-11-26'),8)
            self.assertEqual(app.used(c,'2026-11-28'),12)
            self.assertEqual(app.used(c,'2026-11-27'),9.5)
            self.assertEqual(app.used(c,'2026-11-29'),5)
            self.assertEqual(app.used(c,'2026-11-30'),4)
            self.assertEqual(app.used(c,'2026-12-01'),5)
            self.assertTrue(all(d['used']<=d['capacity'] for d in app.state(c)['days']))
            self.assertEqual(c.execute("SELECT qty FROM orders WHERE customer='Lam'").fetchone()[0],2)
            self.assertEqual(c.execute("SELECT qty FROM orders WHERE customer='Zhou'").fetchone()[0],4)
        self.action(action='collect',id=101)
        with app.connect() as c:
            self.assertEqual(c.execute('SELECT collected FROM orders WHERE id=101').fetchone()[0],1)
        with self.assertRaises(ValueError):self.action(action='advance')
    def test_manual_and_unmatched_cancel(self):
        self.action(action='add',customer='<b>Test</b>',kind='cancel',product='cake',qty=1,pickup='2026-11-25')
        with app.connect() as c:
            r=c.execute('SELECT * FROM requests ORDER BY id DESC LIMIT 1').fetchone()
            self.assertFalse(app.assess(c,r)[0])
        with self.assertRaises(ValueError): self.action(action='add',customer='X',kind='new',product='cake',qty=0,pickup='2026-11-25')
        with self.assertRaises(ValueError): self.action(action='collect',id=112)

if __name__=='__main__':unittest.main(verbosity=2)
