"""

Checking the claims in Theorems 5.10 and 5.14.

To run, do::

  sage: import rbg_cert_final
  sage: import pandas as pd
  sage: df = pd.read_csv('../data/unknown_with_0-friend_final.csv')
  sage: rbg_cert_final.check(df)

You may see the error::

  RuntimeError: The SnapPea kernel was not able to determine if the manifolds are isometric.

If so, just run it again.

"""


import snappy
import plausible_knots
from rbg import RedBlueGreenLink, blue_green_exteriors



def are_isometric(A, B):
    try:
        return A.is_isometric_to(B)
    except RuntimeError:
        A, B = snappy.ManifoldHP(A), snappy.ManifoldHP(B)
        A.randomize(), B.randomize()
        return A.is_isometric_to(B)


def check(df, RBG_encoding='DT'):
    checked = 0
    for i, row in df.iterrows():
        A = snappy.PlausibleKnots[row['base_knot']]
        B = snappy.Link(eval(row['PD_code'])).exterior()
        if RBG_encoding == 'DT':
            L = snappy.Link(row['RBG_DT'])
        else:
            assert RBG_encoding == 'PD'
            L = snappy.Link(eval(row['RBG_PD']))

        L = RedBlueGreenLink(L, eval(row['framing']))
        X, Y = L.green_exterior, L.blue_exterior
        assert ((are_isometric(A, X) and are_isometric(B, Y)) or
                (are_isometric(A, Y) and are_isometric(B, X)))
        assert L.is_super_special()
        print(f'Checked {row["name"]}')
        checked += 1

    return checked


def format_knot(name):
    parts = name.split('_')
    if len(parts) > 1:
        return '$' + parts[0] + '_{' + parts[1] + '}$'
    return '$' + name + '$'


def get_r(framing_string):
    return eval(framing_string)[0][0]


def row_to_table_row(row, note=None):
    ans = format_knot(row['base_knot']) +  ' & '
    ans += row['RBG_DT'][3:-1] + ' & '
    ans += '$' + repr(get_r(row['framing'])) + '$ & '
    if note:
        ans += note
    ans += r'\\'
    return ans


def table_10(df):
    """
    Generates Table 10.
    """
    for i, row in df[df.s_3.notnull() & (df.s_3 != 0)].iterrows():
        print(row_to_table_row(row, "$s_{\F_3} = " + f"{int(row['s_3'])}$"  ))
    row = df[(df.base_knot=='18nh_00010270')].iloc[0]
    print(row_to_table_row(row, 'Sq1_odd'))


def table_11(df):
    """
    Generates Table 11.

    Ribbon disks for::

      ['19nh_000077044', '19nh_000187109', '19nh_003361975']

    were eventually found, so don't appear in the table in the paper.

    """
    mystery = '18nh00000601'

    for i, row in df[df.ribbon==1].iterrows():
        r = get_r(row['framing'])
        if r != 0:
            print(row_to_table_row(row, 'ribbon'))

    others = df[(df.slice==-1)&(df.s_3==0)]
    for i, row in others.iterrows():
         r = get_r(row['framing'])
         if r != 0:
             print(row_to_table_row(row, r'$\svec^\beta \neq 0$'))
