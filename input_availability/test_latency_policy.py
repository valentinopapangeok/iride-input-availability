import unittest
from latency_policy import expected_latency_days
from build_dashboard import css_class_for
class PolicyTests(unittest.TestCase):
 def test_all_products(self):
  for p,n in {'01':7,'02':3,'03':1,'04':1,'05':3,'06':1,'07':8,'08':8,'09':3,'10':7,'11':3}.items():
   self.assertEqual(expected_latency_days(p),n)
 def test_shared_and_full_names(self):
  for p,n in [('01/10',7),('02/11',3),('07/08',8),('OU-S5-02-03',1),('01/03',1)]:self.assertEqual(expected_latency_days(p),n)
 def test_dashboard_boundary(self):
  row={'product':'03','found':'yes'}
  self.assertEqual(css_class_for(row,1),'ok')
  self.assertEqual(css_class_for(row,2),'warn')
  self.assertEqual(css_class_for(row,None),'ok')
 def test_unknown(self):self.assertIsNone(expected_latency_days('99'))
if __name__=='__main__':unittest.main()
