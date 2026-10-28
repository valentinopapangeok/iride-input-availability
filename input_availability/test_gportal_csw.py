import unittest
from unittest.mock import patch,Mock
from gportal_csw import parse_page,search_day
FEED=b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><updated>2026-10-28</updated><mapProjection xmlns="">EQR</mapProjection><link rel="enclosure" href="standard/file.h5"/></entry></feed>'
class CSWTests(unittest.TestCase):
 def test_metadata(self):
  rows,n= parse_page(FEED);self.assertEqual(rows[0]['mapProjection'],'EQR');self.assertEqual(rows[0]['catalogue_updated'],'2026-10-28');self.assertIsNone(n)
 def test_maintenance_is_not_missing(self):
  with self.assertRaises(ValueError):parse_page(b'<html>Maintenance</html>')
 def test_empty_feed(self):self.assertEqual(parse_page(b'<feed xmlns="http://www.w3.org/2005/Atom"/>'),([],None))
 def test_filter(self):
  r=Mock(content=FEED,url='https://csw.gportal.jaxa.jp/csw')
  with patch('gportal_csw.requests.get',return_value=r):
   self.assertEqual(len(search_day(('L3-LST',),'2026-10-26',product_filter=lambda p:p['mapProjection']=='EQR')),1)
   self.assertEqual(search_day(('L3-LST',),'2026-10-26',product_filter=lambda p:p['mapProjection']=='EQA'),[])
 def test_http_error_propagates(self):
  r=Mock();r.raise_for_status.side_effect=RuntimeError('503')
  with patch('gportal_csw.requests.get',return_value=r),self.assertRaises(RuntimeError):search_day(('L3-SST',),'2026-10-26')
if __name__=='__main__':unittest.main()
