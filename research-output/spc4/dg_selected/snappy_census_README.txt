The SnapPy manifold database for plausibly slice knots
======================================================

The PlausibleKnots database consists of those nontrivial prime knots
with at most 19 crossings whose signature is 0 and whose Alexander
polynomial satisfies the Fox-Milnor condition.  To install this Python
module, navigate into this directory in your terminal and do::

  python -m pip install .

If you are using SageMath, replace this with::

  sage -pip install .

Assuming you have installed SnapPy into this Python as well, you can
now start Python and do::

  >>> import snappy
  >>> import plausible_knots
  >>> len(snappy.PlausibleKnots)
  3869541
  >>> K = snappy.PlausibleKnots[100000]; K
  17nh_1645806(0,0)
  >>> K.volume()
  24.3340174921580
  >>> M = snappy.PlausibleKnots['19nh_045358315']
  >>> M.identify()
  [19nh_045358315(0,0)]

The raw data, including the DT code for each knot, is included in CSV format::

  manifold_src/original_manifold_sources/plausible_knots_19.csv.bz2

PD codes for each knot are available in::

  ../data/PD_codes.csv.bz2
