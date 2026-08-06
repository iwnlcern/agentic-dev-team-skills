Summary of data files
=====================

The data files are:

* plausibly_slice.csv: The main file of all 3.9 million plausibly
  slice knots with at most 19 crossings and their known properties
  (slice/ribbon, etc.) and invariants.  For more on its contents,
  see::

    plausibly_column_descript.txt

* plausibly_slice_16.csv: The subset of the last file with just the
  40,274 knots of at most 16 crossings.

* plausibly_unknown.csv: The subset of plausibly_slice.csv where the
  topological or smooth slice status is unknown.  The PD code for
  each of these 11,679 knot is included as the final column.

* PD_codes.csv: contains the planar description (PD) codes for each
  knot, as described in https://katlas.org/wiki/Planar_Diagrams
  Alternatively, you can find a more concise DT code for each knot
  in::

    ../snappy_census/manifold_src/original_manifold_sources/plausible_knots_19.csv.bz2

* plausibly_hyperbolic.csv: Various hyperbolic invariants of these
  knots, used in Section 5 of the paper.  See::
    
    plausibly_column_descriptions.txt

  for more on its contents.

* unknown_with_0-friend_final.csv: This has 41 pairs of 0-friends with
  their RBG links. It includes the 36 pairs featured in Section 5.6,
  including those in Tables 10 and 11.

* zero_friends.csv: The 78,507 0-friends in the ZF collection of
  Section 5.3.  For more on its contents, see::

    zero_friends_column_descript.txt

* more_zero_friends.csv: Another 79,667 0-friends of the knots in
  PS_19, namely those where we have a diagram of at most 90 crossings
  and where we don't know if the 0-friend is smoothly slice.


Format
======

Each file is in comma-separated value (.csv) format, a simple
text-based spreadsheet format, compatible with programs such as Excel,
Open Office, Numbers, etc.  Some of these are very large,
e.g. PD_codes.csv is almost 4 million rows and occupies 1 gigabyte
uncompressed, so you may need to use more serious data-wrangling tools
like Pandas in Python to access all of it.  All but the smallest files
have been compressed with bzip2.
