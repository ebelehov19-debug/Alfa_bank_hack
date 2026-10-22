import csv
import tempfile
import unittest
from pathlib import Path
from core import connect, ingest, profile


class ContextTest(unittest.TestCase):
    def test_conflicts_unknown_partners_and_exact_shares(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fixtures = {
                'df_Uid_data_hack.csv': [['SYNTHETIC_U','MAINOKVED_NAME','GROUP_ID'],
                    ['A','Steel','g1'], ['B','Building','g1'], ['C','Oil','g2'], ['C','Gas','g2']],
                'df_Uid_payment_hack.csv': [['PAYER','BENEFICIARY','PAY_AMT','PAY_CNT'],
                    ['B','A','0.1','1'], ['B','A','0.2','2'], ['UNKNOWN','A','0.7','1'], ['A','A','99','1']],
                'df_Fid_Uid_link_hack.csv': [['SYNTHETIC_F','SYNTHETIC_U'], ['F1','A'], ['F1','B']]
            }
            for name, rows in fixtures.items():
                with (root/name).open('w', newline='') as f:
                    csv.writer(f, delimiter=';').writerows(rows)
            db = connect(':memory:')
            quality = ingest(db, root)
            self.assertEqual(quality['companies_with_conflicting_profiles'], 1)
            self.assertEqual(quality['repeated_payment_pairs'], 1)
            context = profile(db, 'A')
            incoming = context['payments']['incoming']
            self.assertEqual(incoming['total_amount'], '1.0')
            self.assertEqual(incoming['sector_coverage'], .3)
            self.assertEqual(context['group_members'], ['B'])
            self.assertEqual(context['shared_person_companies'], ['B'])
            self.assertIsNone(profile(db,'C')['features']['MAINOKVED_NAME'])
            self.assertIsNone(profile(db,'C')['payments']['incoming']['sector_coverage'])
            (root/'df_Uid_payment_hack.csv').write_text('PAYER;BENEFICIARY;PAY_AMT;PAY_CNT\nA;B;NaN;1\n')
            with self.assertRaises(ValueError):
                ingest(db, root)
            self.assertEqual(profile(db,'A')['payments']['incoming']['total_amount'], '1.0')


if __name__ == '__main__':
    unittest.main()
