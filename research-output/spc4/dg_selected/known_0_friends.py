import networkx as nx
import pandas as pd
import snappy
import plausibly_slice

"""
Here are all 101 pairs of 0-friends within the 19-crossings plausibly
slice knots.

The first three are the ones where the 0-surgery is not hyperbolic.
They are verified by the RBG links at the end and the first is also in
[Kegel-Weiss].

The next 95 pairs come from the main 0-friend search, by drilling out
closed geodesics in the hyperbolic 0-surgery.

The last three pairs are those forced by the preceeding but didn't
show up there for unknown reasons.
"""

friends = [('K15n19499', 'K15n153789'),  # Also in Kegel-Weiss
           ('18nh_00000312', '18nh_00000313'),
           ('19nh_000000424', '19nh_000000425'),
           # From main 0-friend search
           ('16n134691', '16n711888'),
           ('16n233419', '18nh_00000538'),
           ('16n306917', '19nh_000007907'),
           ('16n469934', '18nh_00001506'),
           ('16n470341', '19nh_000008920'),
           ('16n524790', '17nh_0001808'),
           ('16n527686', '17nh_0003716'),
           ('16n579950', 'K10n10'),
           ('16n609311', '19nh_000000553'),
           ('16n764301', '19nh_000017525'),
           ('16n79474', '19nh_000001257'),
           ('16n797712', '18nh_00006490'),
           ('16n82396', '17nh_0000029'),
           ('16n86947', '18nh_00000399'),
           ('16n86947', '18nh_00000400'),
           ('16n885312', '17nh_0005045'),
           ('16n953372', 'K15n59479'),
           ('17nh_0000028', 'K11n116'),
           ('17nh_0000109', '18nh_00000305'),
           ('17nh_0000184', '18nh_00000461'),
           ('17nh_0000185', '18nh_00000462'),
           ('17nh_0000263', 'K10n18'),
           ('17nh_0000476', '17nh_0000678'),
           ('17nh_0000661', '18nh_00000551'),
           ('17nh_0000700', '17nh_0001054'),
           ('17nh_0000829', '17nh_0001032'),
           ('17nh_0000899', '18nh_00001274'),
           ('17nh_0000936', '19nh_000001510'),
           ('17nh_0001272', '19nh_000002681'),
           ('17nh_0001717', 'K13n2801'),
           ('17nh_0002333', '18nh_00001601'),
           ('17nh_0003713', '18nh_00002872'),
           ('17nh_0008206', '18nh_00009658'),
           ('17nh_0010655', '19nh_000015308'),
           ('17nh_0017852', '17nh_0020010'),
           ('17nh_0030142', '19nh_000059787'),
           ('18nh_00000032', 'K12n318'),
           ('18nh_00000220', 'K8a16'),
           ('18nh_00000254', '19nh_000000631'),
           ('18nh_00000254', '19nh_000000632'),
           ('18nh_00000399', 'K11n97'),
           ('18nh_00000400', 'K11n97'),
           ('18nh_00000426', '18nh_00000525'),
           ('18nh_00000475', 'K11n37'),
           ('18nh_00000491', 'K12n313'),
           ('18nh_00000583', '18nh_00000632'),
           ('18nh_00001496', 'K13n3162'),
           ('18nh_00001514', '19nh_000003511'),
           ('18nh_00001556', '19nh_000001903'),
           ('18nh_00002512', '19nh_000002209'),
           ('18nh_00002839', '19nh_000005759'),
           ('18nh_00003074', '19nh_000005769'),
           ('18nh_00003670', '18nh_00004745'),
           ('18nh_00004214', '19nh_000005451'),
           ('18nh_00005767', '19nh_000006763'),
           ('18nh_00008095', '19nh_000008719'),
           ('18nh_00009688', 'K15n128054'),
           ('18nh_00010643', '19nh_000017016'),
           ('18nh_00015326', '19nh_000027505'),
           ('18nh_00015470', '19nh_000024040'),
           ('18nh_00015619', '19nh_000020752'),
           ('18nh_00015863', '19nh_000018547'),
           ('18nh_00016727', '19nh_000023288'),
           ('18nh_00019224', '19nh_000029272'),
           ('18nh_00022375', '18nh_00026660'),
           ('18nh_00058028', '19nh_000068322'),
           ('19nh_000000040', 'K13n3158'),
           ('19nh_000000184', 'K8a4'),
           ('19nh_000000410', 'K15n42042'),
           ('19nh_000000414', 'K15n16924'),
           ('19nh_000000415', 'K15n18792'),
           ('19nh_000000631', '19nh_000000632'),
           ('19nh_000000631', 'K11n67'),
           ('19nh_000000632', 'K11n67'),
           ('19nh_000000705', 'K12n430'),
           ('19nh_000000717', '19nh_000001216'),
           ('19nh_000000852', 'K12n414'),
           ('19nh_000002306', '19nh_000003897'),
           ('19nh_000002344', 'K15n108800'),
           ('19nh_000003130', '19nh_000004495'),
           ('19nh_000003154', '19nh_000003570'),
           ('19nh_000004217', 'K13n3084'),
           ('19nh_000004247', 'K15n86663'),
           ('19nh_000005120', '19nh_000007673'),
           ('19nh_000005414', '19nh_000007557'),
           ('19nh_000006013', '19nh_000008858'),
           ('19nh_000006054', 'K13n2787'),
           ('19nh_000006322', '19nh_000006323'),
           ('19nh_000006800', '19nh_000008818'),
           ('19nh_000008444', '19nh_000015267'),
           ('19nh_000029876', '19nh_000038137'),
           ('19nh_000045703', '19nh_000046274'),
           ('19nh_000141792', '19nh_000141793'),
           ('K11n49', 'K15n103488'),
           ('K12n309', 'K14n14254'),
           # Last three are forced by preceding but were missed for
           # some reason.
           ('16n86947', 'K11n97'),
           ('18nh_00000399', '18nh_00000400'),
           ('18nh_00000254','K11n67')]


# First one already known to Kegel-Weiss
RBG_certs_for_non_hyp = \
    [('K15n19499', 'K15n153789', 'DT[rcbekPeRjboKlFnhGcQdAIM]', [(-1, 1), (0, 1), (0, 1)]),
     ('18nh_00000312', '18nh_00000313', 'DT[ycdhmFoQUYhSAJTlnEkXwvCGdIrpBM]', 3*[(0, 1)]),
     ('19nh_000000424', '19nh_000000425', 'DT[ocefdFmkGbECilNdhaOJ]', 3*[(0, 1)])]


def check_non_hyperbolic():
    import rbg
    for A, B, DT, slopes in RBG_certs_for_non_hyp:
        A = snappy.PlausibleKnots[A]
        B = snappy.PlausibleKnots[B]
        RBG = rbg.RedBlueGreenLink(snappy.Link(DT), slopes)
        assert A.is_isometric_to(RBG.blue_exterior)
        assert B.is_isometric_to(RBG.green_exterior)
        assert RBG.is_super_special()
        print(f'Checked {A.name()}(0, 1) = {B.name()}(0, 1) via {DT}')


"""
There are two sets of 4 mutual friends and 89 isolated pairs.
"""
assert len(friends) == 101

known = nx.Graph()
known.add_edges_from(friends)
comps = set(tuple(sorted(comp)) for comp in list(nx.connected_components(known)))

large0 =  ('16n86947', '18nh_00000399', '18nh_00000400', 'K11n97')
large1 =  ('18nh_00000254', '19nh_000000631', '19nh_000000632', 'K11n67')

assert {comp for comp in comps if len(comp) > 2} == {large0, large1}
assert len(known.subgraph(large0).edges) == 6
assert len(known.subgraph(large1).edges) == 6

def verify_completeness_of_0_friend_list(dataframe):
    """
    Check that we have found all pairs of 0-friends within
    PlausibleKnots.    
    """
    df = dataframe
    print(f'Initial knots: {len(df)}')
    cols = ['vol_0_sur', 'genus3', 'fibered']
    for col in cols:
        assert all(df[col].notnull())
    dg = df.groupby(cols, dropna=False)
    interesting = dg.transform('size') > 1
    df = df[interesting]
    print(f'Have potential match per base_cols: {len(df)}')

    for d in [5, 6, 7]:
        col = f'hash_0_sur_{d}'
        assert all(df[col].notnull())
        cols = cols + [col]
        dg = df.groupby(cols, dropna=False)
        interesting = dg.transform('size') > 1
        df = df[interesting]
        print(f'Have potential match through deg {d} hash: {len(df)}')

    dg = df.groupby(cols)
    sizes = dg.size()
    friends = []
    all_cols = cols + [f'hash_0_sur_{d}' for d in [8, 9, 10]]
    for group_key in dg.groups:
        dt = dg.get_group(group_key)
        knots = tuple(sorted(dt.name))
        if knots in comps:
            # print(f'0-friends: {knots}')
            friends.append(knots)
        else:
            for col in all_cols[-3:]:
                assert (all(dt[col].isnull()) or all(dt[col].notnull()))
            assert not any(dt.groupby(all_cols).size() > 1)

    # Double check there's no information to be gained here.
    for clique in friends:
        dt = df[df.name.isin(clique)]
        # top_slice same as slice except for ('19nh_000003154', '19nh_000003570')
        vals = set(dt.slice) | set(dt.ribbon)
        assert len(vals) == 1 and vals.pop() != 0
        vals = set(dt.top_slice)
        assert len(vals) == 1 and vals.pop() != 0
            
    return set(friends) == comps
