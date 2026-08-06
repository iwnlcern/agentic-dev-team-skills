====
Code
====

The code requires `SnapPy <https://snappy.computop.org>`_ installed in
`SageMath <https://sagemath.org>`_.


Parts added to SnapPy
=====================

Key parts of our code have been incorporated into the development
version of SnapPy.  These will be part of SnapPy 3.3, which should
come out in January 2026.  Until then, you can install the development
version into SageMath by starting a Sage console and doing::
  
  sage: %pip install https://github.com/3-manifolds/snappy_manifolds/zipball/master
  sage: %pip install https://github.com/3-manifolds/Spherogram/zipball/master
  sage: %pip install https://github.com/3-manifolds/SnapPy/zipball/master

Then quit and restart Sage, and try::

  sage: import snappy
  sage: snappy.__version__

You should get a response involving '3.3', such as '3.3a1'.

Some additions include:

1. The Link class has the method ``ribbon_concordant_links`` that is
   used in Section 2 of the paper.  It adds bands and searches for
   ribbon disks.  To see how to use it, type::

     sage: import snappy
     sage: snappy.Link.ribbon_concordant_links?

   To manually add a band, use ``Link.add_band``.

2. The HKL tests are methods of ``Manifold``. To see the documentation
   do::

     sage: snappy.Manifold.slice_obstruction_HKL?

3. The Fox-Milnor test available for a ``Manifold`` by the
   ``fox_milnor_test`` method.

4. The census of ribbon links mentioned in Section 2.6 is ``RibbonLinks``::

     sage: snappy.RibbonLinks?

   By default, ``RibbonLinks`` is used to accelerate a search for
   ribbon disks in ``ribbon_concordant_links``.

5. The "diagram shaking" of Section 2.5 is the method 
   ``many_diagrams`` of the link class::

     sage: snappy.Link.many_diagrams?

6. You can check one of our ribbon certs using
   ``spherogram.links.bands.verify_ribbon_to_unknot`` as detailed in
   the ``check_ribbon_cert.py`` file below.
     

PlausibleKnots
==============

A SnapPy census of the 3.9 million plausibly slice knots with at most
19 crossings is in the ``snappy_census`` folder.  See the README file
there for how to install.


Code included here
==================

In addition to all the code we added directly to SnapPy, we include
here the following::

1. ``algebraically_slice.py``: A partial implementation of the
   algorithm to determine whether a knot is algebraically slice.
   
2. ``bigons.py`` and ``bigons_antiparallel.py``: Searching for knot
   concordances using the technique of Section 2.6 of the paper.

3. ``check_ribbon_cert.py``: Shows how to verify one of the ribbon
   certificates described in Section 2.10 of the paper.

4. ``find_0_friends.py``: Searching for 0-friends of a given knot as
   in Section 5 of the paper.

5. ``known_0_friends.py``: Details the 101 pairs of 0-friends within
   PS_19.
   
6. ``rbg.py``: Python classes for working with Red-Blue-Green (RBG)
   links, following Manolescu-Piccirillo.  Includes the method of
   Section 5.11 for searching for an RBG associated with a given pair
   of 0-friends.

7. ``rbg_cert_final.py``: Uses ``rbg.py`` to check the claims in our
   Theorems 5.10 and 5.14.

8. ``ribbon_concord_graph.py``: Builds the ribbon concordance graph in
   Section 7 of our paper and uses it to complete the proof of Theorem 1.9.





