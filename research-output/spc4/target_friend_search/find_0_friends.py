"""
Code for finding 0-friends of a hyperbolic knot where the
0-surgery is also hyperbolic.

Example usage::

  sage: run find_0_friends.py
  sage: M = snappy.Manifold('K11n34')
  sage: ans = find_common_zero_surgery_via_words(M, 3)
  sage: ans
  [('dbEiFJBDeBD',
    (2.5531911870199804+0.6380805791166294j),
    13.2204588705683542563356066383707285409264389131790846521733779,
    'pLvAvMvPQQQbefihmknjmnlnomoedegvexuruicgosw_baab')]
  sage: E = snappy.Triangulation(ans[0][-1])
  sage: E.num_tetrahedra()
  15
  sage: L = E.exterior_to_link()
  sage: L
  <Link: 1 comp; 38 cross>

"""

import snappy
import sys


def is_three_sphere(manifold):
    """
    True means the manifold is definitely S^3.

    False means it is *likely* not S^3.
    """
    T = manifold
    order = T.homology().order()
    if order == 'infinite' or order > 1:
        return False
    G = snappy.Manifold(T)
    if G.solution_type(enum=True) == 1:
         return False
    for i in range(2):
        if T.fundamental_group().num_generators() == 0:
            return True
        F = T.filled_triangulation()
        if F.fundamental_group().num_generators() == 0:
            return True
        T.randomize()
    return False


def is_knot_exterior(manifold):
    """
    Here, we assume any S^3 slope has length < 4 rather than the
    proven bound of 6.  (The longest known such slope has length 3.3)
    """
    assert manifold.num_cusps() == 1
    if manifold.homology().elementary_divisors() != [0]:
        return False

    try:
        slopes = manifold.short_slopes(4.0)[0]
    except RuntimeError:
        slopes = []

    (a, b) = manifold.homological_longitude()
    slopes = [s for s in slopes if abs(a*s[1] - b*s[0]) == 1]
    for s in slopes:
        M = snappy.Triangulation(manifold)
        M.dehn_fill(s)
        if is_three_sphere(M):
            return s
    return False


def safe_is_isometric_to(A, B):
    """
    Work around when SnapPy is indecisive about when two manifolds are
    the same.
    """
    A, B = A.copy(), B.copy()
    if A.solution_type(enum=True) == 1 and B.solution_type(enum=True) == 1:
        vol_A, vol_B = A.volume(), B.volume()
        RR = vol_A.parent()
        assert vol_A.prec() == vol_B.prec()
        if abs(A.volume() - B.volume()) > RR(2)**(-RR(0.75)*RR.prec()):
            return False
        for i in range(10):
            try:
                return A.is_isometric_to(B)
            except RuntimeError:
                A.randomize(), B.randomize()
        return False


def safe_length_spectrum(manifold, max_len):
    """
    Do a little randomization for when SnapPy struggles to find the
    Dirichlet domain.
    """
    M = manifold.copy()
    ans = None
    for i in range(5):
        try:
            ans = M.length_spectrum(max_len, grouped=False, include_words=True)
            break
        except RuntimeError:
            M.randomize()
    return ans


def find_common_zero_surgery_via_words(manifold, max_len=3.0, min_len=0.0):
    """
    The main function we used to find 0-friends as described in
    Section 5 of the paper.  For a knot K, let Z_K be the 0-surgery.

    Returns a list of 4-tuples with entries encoding the 0-friend K'.

    1. Word in pi_1(Z_K) describing the geodesic that was drilled.

    2. The complex length of said geodesic.

    3. The volume of the exterior of K',

    4. A triangulation of the exterior of K', encoded as a
       triangulation isosig string.

    See the top of this file for an example of use.
    """
    if sys.getrecursionlimit() < 50000:
        sys.setrecursionlimit(50000)
    other_knots = []
    if isinstance(manifold, str):
        manifold = snappy.Manifold(manifold)
    E = snappy.ManifoldHP(manifold)
    M = E.copy()
    M.dehn_fill((0, 1))
    if M.solution_type(enum=True)==1:
        G = M.fundamental_group(False, False, False)
        phi = snappy.snap.nsagetools.MapToFreeAbelianization(G)
        geodesics = safe_length_spectrum(M, max_len)
        if geodesics is None:
            return
        for g in geodesics:
            if g.length.real() < min_len:
                continue
            # The word should represent the generator of H_1 = Z
            if abs(phi(g.word)[0]) != 1:
                continue
            F = M.drill_word(g.word).filled_triangulation()
            if F.solution_type(enum=True) in [1, 2]:
                if safe_is_isometric_to(E, F):
                    continue
                slope = is_knot_exterior(F)
                if slope:
                    vol = F.volume()
                    ident = F.identify()
                    F.dehn_fill(slope)
                    F.set_peripheral_curves('fillings')
                    F.dehn_fill((0,0))
                    F.randomize()
                    other_knots.append((g.word,
                                        complex(g.length),
                                        F.volume(),
                                        F.triangulation_isosig()))
        return other_knots
