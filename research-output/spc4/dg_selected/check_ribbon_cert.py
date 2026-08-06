"""
This code demonstrates how to check a ribbon cert in the
format of Section 2.10 of our paper.  Example usage::

  sage: run check_ribbon_cert.py
  sage: check_cert('K15n165174', cert_K15n165174)
  True
  sage: print_cert_info(K15n165174_cert)
  Diagram 1 has PD code:
      [(5, 1, 6, 0), ..., (27, 21, 28, 20)]

  Band 1 is '223505_1_1' which decompresses to:
      ([(1, 1), (13, 1), (8, 2)], [True], 1)

  Diagram 2 has PD code:
      [(19, 16, 20, 17), ..., (13, 0, 14, 1)]

  Band 2 is '14170f_1_1' which decompresses to:
      ([(3, 3), (5, 3), (5, 0)], [True], 1)

  Final Diagam is: unknot

Some certs end not with the unknot but with a link from
snappy.RibbonLinks, all of which are strongly smoothly slice::

  sage: check_cert('16n2606', cert_16n2606)
  True
  sage: print_cert_info(cert_16n2606)
  Diagram 1 has PD code:
      [(33, 17, 0, 16), ..., (13, 1, 14, 0)]

  Band 1 is '32391e0d_1_1' which decompresses to:
      ([(3, 1), (7, 2), (14, 1), (12, 2)], [True, False], 1)

  Final Diagam is: ribbon_2_18_86ba6dfc


The function ``check_hard_certs_through_16_crossings`` checks the
almost 1,000 hard ribbon certs needed for knots with at most 16
crossings.

Finally, we give an example of a ribbon cert that we struggled to find
in ``def check_really_tricky_cert``.

"""

import snappy
import plausible_knots
from spherogram.links.bands.core import uncompress_band_spec
from spherogram.links.bands import verify_ribbon_to_unknot

cert_K12a990 = "[[(5,1,6,0),(1,20,2,21),(9,2,10,3),(3,10,4,11),(19,4,20,5),(21,7,22,6),(7,14,8,15),(13,8,14,9),(11,19,12,18),(17,13,18,12),(15,23,16,22),(23,17,0,16)],'080f0e_1_0',[(11,2,0,3),(1,10,2,11),(5,9,6,8),(7,5,8,4),(3,7,4,6),(9,0,10,1)],'0d02_0_-1','unknot']"

cert_K15n165174 = "[[(5,1,6,0),(22,2,23,1),(2,22,3,21),(3,13,4,12),(13,5,14,4),(15,6,16,7),(7,24,8,25),(25,8,26,9),(9,18,10,19),(19,10,20,11),(11,28,12,29),(29,14,0,15),(23,17,24,16),(17,27,18,26),(27,21,28,20)],'223505_1_1',[(19,16,20,17),(15,20,16,21),(12,22,13,21),(22,8,23,7),(6,18,7,23),(17,2,0,3),(1,14,2,15),(8,12,9,11),(10,6,11,5),(4,10,5,9),(3,18,4,19),(13,0,14,1)],'14170f_1_1','unknot']"

cert_16n2606 = "[[(33,17,0,16),(8,17,9,18),(3,15,4,14),(15,5,16,4),(2,21,3,22),(31,28,32,29),(10,28,11,27),(19,33,20,32),(18,9,19,10),(12,7,13,8),(5,21,6,20),(26,12,27,11),(25,30,26,31),(23,7,24,6),(29,24,30,25),(22,1,23,2),(13,1,14,0)],'32391e0d_1_1','ribbon_2_18_86ba6dfc']"


def print_cert_info(ribbon_cert):
    if isinstance(ribbon_cert, str):
        ribbon_cert = eval(ribbon_cert)
    diags = ribbon_cert[0::2]
    bands = ribbon_cert[1::2]
    last_diag = diags[-1]
    i = 1 # follow paper's notation
    for D, B in zip(diags, bands):
        print(f'Diagram {i} has PD code:')
        print(f'    {D}\n')
        print(f"Band {i} is '{B}' which decompresses to:")
        U = uncompress_band_spec(B)
        print(f'    {U}\n')
        i += 1

    print(f'Final Diagam is: {last_diag}')


def check_cert(knot_name, ribbon_cert, max_tries=10):
    """
    When an intermediate link L_i in the ribbon cert is not
    hyperbolic, SnapPy has difficulty checking that adding the
    preceding band produces L_i.  Moreover, SnapPy's link
    simplification is not deterministic, so we don't necessarily land
    on the same PD code for L_i each time.  To deal with these
    situations, we run the test several times.
    """
    if isinstance(ribbon_cert, str):
        ribbon_cert = eval(ribbon_cert)
    K = snappy.PlausibleKnots[knot_name].link()
    for i in range(max_tries):
        checked = verify_ribbon_to_unknot(K, ribbon_cert)
        if checked:
            break
    return checked


def check_hard_certs_through_16_crossings():
    import pandas as pd
    df = pd.read_csv('../data/plausibly_slice_16.csv')
    df = df[df.ribbon_cert.notnull()]
    print(f'Will now check {len(df)} ribbon certificates')
    for i, row in df.iterrows():
        name = row['name']
        success = check_cert(name, row['ribbon_cert'])
        ans = 'OK' if success else 'FAILED!'
        print(f'{name}: {ans}')


def check_really_tricky_cert():
    """
    The knot 19nh_051162051 was one of about 440 that we intially
    identified as slice using the technique of "bigons.py".  We were
    able to find ribbon discs for most of these without too much
    difficulty, but this one held out for some time.  Moreover, we
    didn't realize that the search had succeeded so tried ever more
    elaborate methods which oddly failed.

    Because some of the intermediate links are not hyperbolic, it's
    tricky to check this one.
    """
    cert = "[[(13,1,14,0),(1,13,2,12),(2,30,3,29),(36,3,37,4),(4,35,5,36),(34,5,35,6),(31,7,32,6),(7,14,8,15),(15,8,16,9),(9,24,10,25),(21,10,22,11),(11,18,12,19),(23,17,24,16),(17,23,18,22),(28,19,29,20),(20,27,21,28),(25,33,26,32),(33,27,34,26),(30,0,31,37)],'023324_0_0',[(37,21,38,20),(21,37,22,36),(22,12,23,11),(18,23,19,24),(24,17,25,18),(16,25,17,26),(13,27,14,26),(27,30,28,31),(31,28,32,29),(38,3,39,4),(2,39,3,36),(6,34,7,33),(34,6,35,5),(1,10,2,11),(7,15,8,14),(15,9,16,8),(12,20,13,19),(29,32,30,33),(4,0,5,35),(9,0,10,1)],'1a4718_0_0',[(32,11,33,12),(12,33,13,34),(13,5,14,4),(9,14,10,15),(15,8,16,9),(16,19,17,20),(20,17,21,18),(35,29,32,28),(27,35,28,34),(1,23,2,22),(23,31,24,30),(26,3,27,4),(31,7,0,6),(5,11,6,10),(18,21,19,22),(29,25,30,24),(2,25,3,26),(7,1,8,0)],'2a334627_2_0',[(5,8,6,9),(9,6,10,7),(11,3,0,2),(1,5,2,4),(7,10,8,11),(3,1,4,0)],'040b_0_1','unknot']"
    name = '19nh_051162051'
    print_cert_info(cert)
    print('\nCertificate Checks:', check_cert(name, cert, 1000))

    
