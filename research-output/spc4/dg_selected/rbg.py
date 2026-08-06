"""
Working with Red-Blue-Green links, following Manolescu-Piccirillo:
https://arxiv.org/abs/2102.04391
"""

import snappy
from snappy.snap.nsagetools import MapToFreeAbelianization
from sage.all import vector


def normalize_slope(a, b=None):
    if b is None:
        a, b = a
    if a*b == 0:
        a, b = abs(a), abs(b)
    elif b < 0:
        a, b = -a, -b
    return (a, b)


def invert_perm(perm):
    n = len(perm)
    ans = n*[None]
    for i, j in enumerate(perm):
        ans[j] = i
    return ans


def is_Z_homology_solid_torus(manifold):
    if not True in manifold.cusp_info('is_complete'):
        return False
    return manifold.homology().elementary_divisors() == [0]


def is_three_sphere(manifold):
    """
    True means the manifold is unequivocally the 3-sphere, False means
    likely not but this has not been proven.
    """
    T = manifold
    order = T.homology().order()
    if order == 'infinite' or order > 1:
        return False
    for i in range(2):
        if T.fundamental_group().num_generators() == 0:
            return True
        F = T.filled_triangulation()
        if F.fundamental_group().num_generators() == 0:
            return True
        T.randomize()
    return False


def is_meridian_filling_S3(manifold):
    """
    >>> is_meridian_filling_S3(snappy.Manifold('m004'))
    True
    >>> is_meridian_filling_S3(snappy.Manifold('m007'))
    False
    """
    M = snappy.Triangulation(manifold)
    n = M.num_cusps()
    M.dehn_fill(n*[(1, 0)])
    return is_three_sphere(M)


def is_unlink_exterior(manifold):
    if manifold.solution_type() == 'all tetrahedra positively oriented':
        return False
    return manifold.fundamental_group().num_relators() == 0


def is_solid_torus_with_longitude(manifold, framing):
    """
    >>> A = snappy.Manifold('L2a1(0, 0)(1, 0)')
    >>> is_solid_torus_with_longitude(A, (1, 0))
    False
    >>> is_solid_torus_with_longitude(A, (0, -1))
    True
    >>> A = snappy.Manifold('L2a1(0, 0)(1, 3)')
    >>> is_solid_torus_with_longitude(A, (3, 1))
    True
    """
    if manifold.num_cusps() > 1:
        manifold = manifold.filled_triangulation()
    assert manifold.cusp_info('is_complete') == [True]
    if is_unlink_exterior(manifold):
        hom_long = manifold.homological_longitude()
        return normalize_slope(hom_long) == normalize_slope(framing)
    return False


def is_unlink(link):
    """
    >>> L = snappy.Link('L14n1575')
    >>> is_unlink(L.sublink([0]))
    True
    >>> is_unlink(L.sublink([1]))
    False
    """
    if len(link.crossings) == 0:
        return True
    return is_unlink_exterior(link.exterior())


def is_hopf_link_exterior(manifold):
    if (manifold.num_cusps() != 2 or
        manifold.solution_type() == 'all tetrahedra positively oriented'):
        return False
    G = manifold.fundamental_group()
    if G.num_generators() > 2 or G.num_relators() != 1:
        return False
    targets = ['abAB', 'bABa', 'ABab', 'BabA', 'aBAb', 'BAba', 'AbaB', 'baBA']
    return G.relators()[0] in targets


def is_hopf_link(link):
    L = link.copy()
    if L.unlinked_unknot_components > 0:
        return False
    L.simplify()
    if len(L.link_components) == 2 and len(L.crossings) == 2:
        return True
    return is_hopf_link_exterior(L.exterior())


def reorder_link_components(link, perm):
    """
    This link has three components, which are in order a trefoil, the
    figure 8, and an unknot.

    >>> L0 = snappy.Link('DT[uciicOFRTIQKUDsMpgBelAnjCH.000001011100110110010]')
    >>> [len(L0.sublink([i]).crossings) for i in range(3)]
    [3, 4, 0]

    perm is the permutation of {0, 1, ... , num_comps - 1} where
    perm[old_comp_index] = new_comp_index.

    >>> L1 = reorder_link_components(L0, [1, 2, 0])
    >>> [len(L1.sublink([i]).crossings) for i in range(3)]
    [0, 3, 4]
    >>> L2 = reorder_link_components(L0, [2, 0, 1])
    >>> [len(L2.sublink([i]).crossings) for i in range(3)]
    [4, 0, 3]
    """
    n = len(link.link_components)
    assert len(perm) == n and link.unlinked_unknot_components == 0
    L = link.copy()
    component_starts = n*[None]
    for a, b in enumerate(perm):
         component_starts[b] = L.link_components[a][0]
    L._build_components(component_starts)
    return L


def isometry_preserving_curves(M0, mu0, M1, mu1):
    """
    >>> A = snappy.Manifold('L14n62484')
    >>> B = A.copy(); B._reindex_cusps([3, 2, 1, 0])
    >>> mu_A = [(1, 0), (1, 1), (1, 2), (1, 3)]
    >>> mu_B = [(1, 3), (1, 2), (1, 1), (1, 0)]
    >>> iso = isometry_preserving_curves(A, mu_A, B, mu_B)
    >>> iso.cusp_images()
    [3, 2, 1, 0]
    """
    for iso in M0.is_isometric_to(M1, return_isometries=True):
        perm = iso.cusp_images()
        maps = iso.cusp_maps()
        match = True
        for i0, m0 in enumerate(mu0):
            m1 = mu1[perm[i0]]
            v = maps[i0] * vector(m0)
            if v != vector(m1) and v != -vector(m1):
                match = False
                break
        if match:
            return iso


class RedBlueGreenLink:
    """
    Here is the example of Figure 4 of [MP]:

    >>> L0 = snappy.Link('DT: pcdicelHnacPJMBogIdFk')
    >>> L = RedBlueGreenLink(L0, [(1, 1), (0, 1), (0, 1)]); L
    <RBG link of 16 crossings with r=(1, 1), b=(0, 1), g=(0, 1)>
    >>> snappy.HTLinkExteriors.identify(L.blue_exterior)
    K12n309(0,0)
    >>> snappy.HTLinkExteriors.identify(L.green_exterior)
    K14n14254(0,0)

    Here is Figure 4(h) from [Piccirillo, Annals, 2020].

    >>> dt = ('[(-54,44,22,38,-60,32,48),'
    ...       '(52,-46,56,50,6,62,-40,-34),'
    ...       '(42,-28,12,58,-24,10,-36,-18,2,-30,20,-14,16,-4,-64,26,-8)]')
    >>> L1 = snappy.Link('DT: ' + dt)
    >>> L = RedBlueGreenLink(L1, [(0, 1), (0, 1), (0, 1)]); L
    <RBG link of 32 crossings with r=(0, 1), b=(0, 1), g=(0, 1)>
    >>> snappy.HTLinkExteriors.identify(L.blue_exterior)
    K11n34(0,0)
    >>> L.is_super_special()
    True

    Here is Figure 4(g) from the same paper:

    >>> L2 = snappy.Link('DT: tcgbkkTlinRPcjGhaQemFsDOB')
    >>> L = RedBlueGreenLink(L2, [(0, 1), (-2, 1), (0, 1)]); L
    <RBG link of 20 crossings with r=(0, 1), b=(-2, 1), g=(0, 1)>
    >>> snappy.HTLinkExteriors.identify(L.blue_exterior)
    K11n34(0,0)
    >>> L.is_super_special()
    False
    """
    def __init__(self, link, framings):
        self.link = link.copy()
        self.framings = [normalize_slope(s) for s in framings]
        self.exterior = self.link.exterior()
        r, b, g = self.framings
        E = self.exterior.copy()
        E.dehn_fill([r, (0, 0), g])
        self.blue_exterior = E.filled_triangulation()
        E = self.exterior.copy()
        E.dehn_fill([r, b, (0, 0)])
        self.green_exterior = E.filled_triangulation()
        self._verify()
        self._blue_knot = None
        self._green_knot = None

    def _verify(self):
        if not (is_meridian_filling_S3(self.blue_exterior) and
                is_meridian_filling_S3(self.green_exterior)):
            raise ValueError('Not an RBG link')

        E = self.exterior.copy()
        E.dehn_fill(self.framings)
        if E.homology().elementary_divisors() != [0]:
            raise ValueError('Not an RBG link')

    def blue_knot(self):
        if self._blue_knot is None:
            self._blue_knot = self.blue_exterior.exterior_to_link()
        return self._blue_knot

    def green_knot(self):
        if self._green_knot is None:
            self._green_knot = self.green_exterior.exterior_to_link()
        return self._green_knot

    def __repr__(self):
        r, b, g = self.framings
        num_cross = len(self.link.crossings)
        return f"<RBG link of {num_cross} crossings with r={r}, b={b}, g={g}>"

    def is_symmetric(self):
        """
        An RBG link is symmetric when S^3_{b, g}(B U G) is also S^3.
        """
        E = self.exterior.copy()
        r, b, g = self.framings
        E.dehn_fill([(1, 0), b, g])
        return is_three_sphere(E)

    def is_super_special(self):
        """
        Checks if the RBG link has (red U blue) and (reg U green) both
        Hopf links and b = g = 0.  This in particular implies all
        three components are unknotted.  This is stronger than the
        notion of special RBG links from [MP] which instead requires
        that (red U blue) and (reg U green) are (red U meridian_red)
        and b = g = 0.

        Here is a four-crossing link where (red U blue) and (reg U green)
        are Hopf links but (blue U green) is the unlink.

        >>> A0 = snappy.Link([(4,6,1,5), (5,3,6,4), (8,3,7,2), (1,8,2,7)])
        >>> A = RedBlueGreenLink(A0, [(0, 1), (0, 1), (0, 1)])
        >>> A.is_super_special()
        True
        """
        r, b, g = self.framings
        if self.framings[1:] != [(0, 1), (0, 1)]:
            return False
        L = self.link
        for i in range(3):
            if not is_unlink(L.sublink([i])):
                return False

        return is_hopf_link(L.sublink([0, 1])) and is_hopf_link(L.sublink([0, 2]))


class BlueGreenExterior:
    """
    The exterior of two component link in the 3-sphere encoding a pair
    knots with the same 0-surgery.

    >>> E = snappy.Manifold('DT: pcdicelHnacPJMBogIdFk')
    >>> E.dehn_fill((1, 1), 0)
    >>> E = E.filled_triangulation()
    >>> BGE = BlueGreenExterior(E, (1, 0), (0, 1), (1, 0), (0, 1))

    Looking at Conway knot:

    >>> isosig = 'svLLAzvwwQQQQcgfhillpnmrqproqoqrwtxauxxiauqafrwojsn_aBbaaBba'
    >>> E = snappy.Manifold(isosig)
    >>> BGE = BlueGreenExterior(E, (1, 0), (0, 1), (1, 1), (1, 0))
    >>> B = BGE.blue_exterior()
    >>> B.is_isometric_to(snappy.Manifold('K11n34'))
    True
    >>> G = BGE.green_exterior()
    >>> piccirillo_partner = 'sLLvLvzPzQQQQcdgnmpkqkpqrmnqoorrhsgpxhpxudhbnwsssko_bBba'
    >>> G.is_isometric_to(snappy.Manifold(piccirillo_partner))
    True

    Now K13n866:

    >>> isosig = 'tLLzALwwzPAPQkceeffhjimnqporssrsqriiaedsdhaammeqaeaqoe_BbabaBBa'
    >>> E = snappy.Manifold(isosig)
    >>> BGE = BlueGreenExterior(E, (1, 0), (0, 1), (0, 1), (1, 0))


    K15n25044:

    >>> isosig = 'xLLLLvPzzzwQAAQQcacfgimjnpptpqsovsusvwvwwnkvjapcaeattpcbiocfhtdpl_BbCbaBbb'
    >>> E = snappy.Manifold(isosig)
    >>> BGE = BlueGreenExterior(E, (1, 0), (0, 1), (1, 1), (1, 0))
    >>> B = BGE.blue_exterior()
    >>> B.is_isometric_to(snappy.Manifold('K15n25044'))
    True

    """
    def __init__(self, manifold, blue_merid, blue_long, green_merid, green_long):
        self.manifold = manifold.copy()
        self.blue_merid = normalize_slope(blue_merid)
        self.blue_long = normalize_slope(blue_long)
        self.green_merid = normalize_slope(green_merid)
        self.green_long = normalize_slope(green_long)
        self._verify()

    def _verify(self):
        # if not is_meridian_filling_S3(self.manifold):
        #    raise ValueError('Meridian filling does not appear to be S^3')

        M = self.manifold.copy()
        M.dehn_fill([self.blue_long, self.green_long])
        if M.homology().elementary_divisors() != [0]:
            raise ValueError('Mutual 0-surgery has wrong homology')

        M = self.manifold.copy()
        M.dehn_fill([self.blue_merid, self.green_long])
        if not is_three_sphere(M):
            raise ValueError('Something wrong with blue knot')

        M = self.manifold.copy()
        M.dehn_fill([self.blue_long, self.green_merid])
        if not is_three_sphere(M):
            raise ValueError('Something wrong with green knot')

    def blue_exterior(self):
        E = self.manifold.copy()
        E.dehn_fill(self.green_long, 1)
        return E.filled_triangulation()

    def green_exterior(self):
        E = self.manifold.copy()
        E.dehn_fill(self.blue_long, 0)
        return E.filled_triangulation()

    def search_for_nice_dual_curves(self, max_segements=12, radius=6.0, only_one_per_length=True):
        M = self.manifold
        for curve in M.dual_curves(max_segements):
            if curve.filled_length.real() > radius:
                break
            print(f'Trying {curve}')
            E = M.drill(curve)
            # Move the drilled cusp to the beginning as it will be the red cusp.
            E._reindex_cusps([1, 2, 0])
            #ans = self._is_RBG_exterior(E)
            ans = self._drilled_manifold_is_super_special(E)
            #ans = self._drilled_certifies_diffeo_trace(E)
            if ans is not None:
                yield ans

    def search_for_nice_words(self, radius=3.0):
        M = snappy.ManifoldHP(self.manifold)
        curves = M.length_spectrum(radius, include_words=True, grouped=False)
        for curve in curves:
            print(f'Trying {curve}')
            E = M.drill_word(curve.word)
            # Move the drilled cusp to the beginning as it will be the red cusp.
            E._reindex_cusps([1, 2, 0])
            ans = self._drilled_certifies_diffeo_trace(snappy.Manifold(E))
            if ans is not None:
                return ans

    def _is_RBG_exterior(self, manifold):
        E1 = manifold.copy()
        E1.dehn_fill(self.blue_merid, 1)
        E1.dehn_fill(self.green_merid, 2)
        poss_red_merid = manifold.short_slopes(12, first_cusps=[0])[0]
        for red_merid in poss_red_merid:
            framing = [red_merid, self.blue_merid, self.green_merid]
            E1.dehn_fill(red_merid, 0)
            if is_three_sphere(E1):
                L = E1.exterior_to_link(check_answer=False)
                L = L.mirror()
                X = L.exterior()
                iso = isometry_preserving_curves(manifold, framing, X, 3*[(1, 0)])
                L = reorder_link_components(L, invert_perm(iso.cusp_images()))
                X = L.exterior()
                iso = isometry_preserving_curves(manifold, framing, X, 3*[(1, 0)])
                assert iso.cusp_images() == [0, 1, 2]
                maps = iso.cusp_maps()
                new_red_long = maps[0]*vector((1, 0))
                new_blue_long = maps[1]*vector(self.blue_long)
                new_green_long = maps[2]*vector(self.green_long)
                framing = [new_red_long, new_blue_long, new_green_long]
                framing = [normalize_slope(slope) for slope in framing]
                return RedBlueGreenLink(L, framing)

    def _drilled_certifies_diffeo_trace(self, manifold):
        """
        Assumes the cusps are in red-blue-green order, that the
        longitude for red is (1, 0), but the meridian for red is
        unknown.

        Uses the following from Lisa Piccirillo's email of 2022-11-17:

        Lemma. If R is unknotted and 0-framed then the traces of K_B
        and K_G have diffeomorphic traces.

        Proof: If R is a 0-framed unknot and r=0, since S^3_{0,b}(R,B)
        is S^3, we can use some version of property R to conclude that
        B is isotopic to S^1\times{pt} in the S^1\times S^2 given by
        0-surgery on R. So in particular, after handle slides B over
        R, we can assume B and R are a hopf link, with R
        0-framed. Same with R and G. Lemma 7.1 of the paper with
        Ciprian points out that slides of B and G over R don't change
        the knots K_B and K_G. So now you’re special RBG with R a
        0-framed unknot, and lemma 4.3 applies.
        """

        # Check whether R is a 0-framed unknotted
        E1 = manifold.copy()
        E1.dehn_fill(self.blue_merid, 1)
        E1.dehn_fill(self.green_merid, 2)
        if is_solid_torus_with_longitude(E1, (1, 0)):
            poss_red_merid = manifold.short_slopes(12, first_cusps=[0])[0]
            for red_merid in poss_red_merid:
                if abs(red_merid[1]) == 1:
                    framing = [red_merid, self.blue_merid, self.green_merid]
                    E1.dehn_fill(red_merid, 0)
                    L = E1.exterior_to_link(check_answer=False)
                    L = L.mirror()
                    X = L.exterior()
                    iso = isometry_preserving_curves(manifold, framing, X, 3*[(1, 0)])
                    L = reorder_link_components(L, invert_perm(iso.cusp_images()))
                    X = L.exterior()
                    iso = isometry_preserving_curves(manifold, framing, X, 3*[(1, 0)])
                    assert iso.cusp_images() == [0, 1, 2]
                    maps = iso.cusp_maps()
                    new_red_long = maps[0]*vector((1, 0))
                    new_blue_long = maps[1]*vector(self.blue_long)
                    new_green_long = maps[2]*vector(self.green_long)
                    framing = [new_red_long, new_blue_long, new_green_long]
                    framing = [normalize_slope(slope) for slope in framing]
                    return RedBlueGreenLink(L, framing)


            raise ValueError('Something is not working out...')


        # If that fails, possibly both B and G are 0-framed unknots
        if manifold.solution_type() != 'all tetrahedra positively oriented':
            return

        poss_red_merid = manifold.short_slopes(12, first_cusps=[0])[0]
        for merid in poss_red_merid:
            E2 = manifold.copy()
            framing = [merid, self.blue_merid, self.green_merid]
            E2.dehn_fill(framing)
            if is_three_sphere(E2):
                # Now we have to check that it's a *symmetric* RBG link.
                E3 = manifold.copy()
                E3.dehn_fill([merid, self.blue_long, self.green_long])
                if is_three_sphere(E3):
                    E4 = manifold.copy()
                    E4.dehn_fill([merid, (0, 0), self.green_merid])
                    if not is_solid_torus_with_longitude(E4, self.blue_long):
                        continue
                    E5 = manifold.copy()
                    E5.dehn_fill([merid, self.blue_merid, (0, 0)])
                    if is_solid_torus_with_longitude(E5, self.green_long):
                        L = E2.exterior_to_link(check_answer=False)
                        L = L.mirror()
                        X = L.exterior()
                        iso = isometry_preserving_curves(manifold, framing, X, 3*[(1, 0)])
                        L = reorder_link_components(L, invert_perm(iso.cusp_images()))
                        X = L.exterior()
                        iso = isometry_preserving_curves(manifold, framing, X, 3*[(1, 0)])
                        assert iso.cusp_images() == [0, 1, 2]
                        maps = iso.cusp_maps()
                        new_red_long = maps[0]*vector((1, 0))
                        new_blue_long = maps[1]*vector(self.blue_long)
                        new_green_long = maps[2]*vector(self.green_long)
                        framing = [new_red_long, new_blue_long, new_green_long]
                        framing = [normalize_slope(slope) for slope in framing]
                        return RedBlueGreenLink(L, framing)

    def _drilled_manifold_is_super_special(self, manifold):
        """
        Assumes the cusps are in red-blue-green order, that the
        longitude for red is (1, 0), but the meridian for red is
        unknown.  See RedBlueGreenLink.is_super_special for the
        definition.
        """
        E = manifold.copy()
        E1 = E.copy()
        E1.dehn_fill(self.blue_merid, 1)
        E1 = E1.filled_triangulation()
        if not is_hopf_link_exterior(E1):
            return

        E2 = E.copy()
        E2.dehn_fill(self.green_merid, 2)
        E2 = E2.filled_triangulation()
        if not is_hopf_link_exterior(E2):
            return

        print('    Found fillings giving Hopf links')
        if E.solution_type().startswith('contains'):
            return

        poss_red_merid = E.short_slopes(12, first_cusps=[0])[0]
        for merid in poss_red_merid:
            E3 = E.copy()
            framing = [merid, self.blue_merid, self.green_merid]
            E3.dehn_fill(framing)
            if is_three_sphere(E3):
                print('    Found link exterior, checking framing')
                framing_ok = True
                targets = [(1, 0),
                           normalize_slope(self.blue_long),
                           normalize_slope(self.green_long)]

                for i in range(1, 3):
                    E4 = E3.copy()
                    E4.dehn_fill((0, 0), i)
                    longitude = normalize_slope(E4.homological_longitude())
                    if longitude != targets[i]:
                        framing_ok = False
                        break

                if not framing_ok:
                    print('    Framing is not super-special')
                    continue

                print('    Framing good, finding the link diagram')
                L = E3.exterior_to_link(check_answer=False)
                L.simplify('global')
                L = L.mirror()
                X = L.exterior()
                iso = isometry_preserving_curves(E, framing, X, 3*[(1, 0)])
                L = reorder_link_components(L, invert_perm(iso.cusp_images()))
                X = L.exterior()
                iso = isometry_preserving_curves(E, framing, X, 3*[(1, 0)])
                assert iso.cusp_images() == [0, 1, 2]
                maps = iso.cusp_maps()
                new_red_long = maps[0]*vector((1, 0))
                new_blue_long = maps[1]*vector(self.blue_long)
                new_green_long = maps[2]*vector(self.green_long)
                framing = [new_red_long, new_blue_long, new_green_long]
                framing = [normalize_slope(slope) for slope in framing]
                ans = RedBlueGreenLink(L, framing)
                print(ans)
                return ans


def blue_green_exteriors(blue_exterior, blue_merid,
                         green_exterior, green_merid):
    blue_long = blue_exterior.homological_longitude()
    green_long = green_exterior.homological_longitude()
    green_vol = green_exterior.volume()
    for d in blue_exterior.dual_curves(12):
        print(d)
        E = blue_exterior.drill(d)
        E1 = E.copy()
        E1.dehn_fill(blue_long, 0)
        E1 = E1.filled_triangulation()
        if is_Z_homology_solid_torus(E1) and abs(E1.volume() - green_vol) < 1e-8:
            if E1.solution_type().startswith('contains'):
                continue
            isos = green_exterior.is_isometric_to(E1, True)
            if isos:
                iso = isos[0]
                A = iso.cusp_maps()[0]
                new_green_merid = normalize_slope(A*vector(green_merid))
                new_green_long = normalize_slope(A*vector(green_long))
                E = snappy.Manifold(E)
                BGE = BlueGreenExterior(E, blue_merid, blue_long, new_green_merid, new_green_long)
                print(f'FOUND: Volume {BGE.manifold.volume()}')
                for RBG in BGE.search_for_nice_dual_curves():
                    yield RBG


def blue_green_exteriors_alt(blue_exterior, blue_merid,
                             green_exterior, green_merid,
                             radius=4):
    """
    sage: blue_ex = snappy.PlausibleKnots['17nh_0016322']
    sage: green_ex = snappy.Manifold('vLLLLwwAwLAAPQQccegfhkjloponstqrustuulnanvgptlaamvagcrdbjlm_aBBb')
    sage: gen = blue_green_exteriors_alt(blue_ex, (1, 0), green_ex, (1, 0))
    sage: rbg = next(gen)
    ...
    <RBG link of 42 crossings with r=(0, 1), b=(0, 1), g=(0, 1)>

    """
    blue_long = blue_exterior.homological_longitude()
    green_long = green_exterior.homological_longitude()
    green_vol = green_exterior.volume()
    curves = blue_exterior.length_spectrum(radius, include_words=True, grouped=False)
    for curve in curves:
        print(f'Trying {curve}')
        E = blue_exterior.drill_word(curve.word)
        E1 = E.copy()
        E1.dehn_fill(blue_long, 0)
        E1 = E1.filled_triangulation()
        if is_Z_homology_solid_torus(E1) and abs(E1.volume() - green_vol) < 1e-8:
            isos = green_exterior.is_isometric_to(E1, True)
            if isos:
                iso = isos[0]
                A = iso.cusp_maps()[0]
                new_green_merid = normalize_slope(A*vector(green_merid))
                new_green_long = normalize_slope(A*vector(green_long))
                E = snappy.Manifold(E)
                BGE = BlueGreenExterior(E, blue_merid, blue_long, new_green_merid, new_green_long)
                print(f'FOUND: Volume {BGE.manifold.volume()}')
                for RBG in BGE.search_for_nice_dual_curves():
                    yield RBG


if __name__ == '__main__':
    import doctest
    print(doctest.testmod())
