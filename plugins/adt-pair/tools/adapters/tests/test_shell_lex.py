import unittest
from tools.adapters import shell_lex


class ShellLexTests(unittest.TestCase):
    def test_lex_fd_duplication_is_one_operator(self):
        self.assertEqual([t for t in shell_lex.lex("x >f 2>&1") if t[2]],
                         [(">", False, True), (">&", False, True)])

    def test_lex_separated_gt_amp_stays_two_operators(self):
        self.assertEqual([t[0] for t in shell_lex.lex("x > &1") if t[2]], [">", "&"])

    def test_lex_background_ampersand_unchanged(self):
        self.assertEqual(shell_lex.lex("x & y")[1], ("&", False, True))

    def test_lex_quoted_and_escaped_dup_are_words(self):
        for command in ("'>&'", r"\>\&"):
            self.assertEqual(shell_lex.lex(command), [(">&", True, False)])

    def test_lex_spans_projects_to_lex(self):
        for command in ("x >f 2>&1", "x 2 >& 1", "x 'a b' c", 'K="a b" x',
                        "a;b\nc", "echo '' # comment", r"x a\ b", "x & y"):
            with self.subTest(command=command):
                spans = shell_lex.lex_spans(command)
                self.assertEqual([t[:3] for t in spans], shell_lex.lex(command))
                self.assertTrue(all(0 <= t[3] < t[4] <= len(command) for t in spans))
        self.assertEqual(shell_lex.lex_spans("x 2>&1"), [
            ("x", False, False, 0, 1), ("2", False, False, 2, 3),
            (">&", False, True, 3, 5), ("1", False, False, 5, 6)])
        self.assertEqual(shell_lex.lex_spans("x 2 >&1")[2][3], 4)
        self.assertIsNone(shell_lex.lex_spans("x 'oops"))

    def texts(self, cmd):
        return [(t, q, o) for t, q, o in shell_lex.lex(cmd)]

    def test_quotes_escapes_and_operators(self):
        self.assertEqual(
            self.texts("a 'b;c' d"),
            [("a", False, False), ("b;c", True, False), ("d", False, False)],
        )
        self.assertEqual(
            self.texts("a;b"),
            [("a", False, False), (";", False, True), ("b", False, False)],
        )
        self.assertEqual(
            self.texts("x >f 2>&1"),
            [
                ("x", False, False),
                (">", False, True),
                ("f", False, False),
                ("2", False, False),
                (">&", False, True),
                ("1", False, False),
            ],
        )
        self.assertEqual(
            self.texts("p '>x.md'"), [("p", False, False), (">x.md", True, False)]
        )
        self.assertEqual(
            self.texts("K='a b' x"), [("K=a b", 2, False), ("x", False, False)]
        )
        self.assertEqual(
            self.texts('K="a b" x'), [("K=a b", 2, False), ("x", False, False)]
        )
        self.assertEqual(
            self.texts('"K=v" x'), [("K=v", True, False), ("x", False, False)]
        )
        self.assertEqual(self.texts("a'b'c"), [("abc", 1, False)])
        self.assertEqual(self.texts("'a'bc"), [("abc", True, False)])
        self.assertEqual(self.texts("ab\\=c"), [("ab=c", 2, False)])
        self.assertEqual(
            [
                shell_lex.unquoted_prefix(t, q)
                for t, q, _ in self.texts("K='a b' \"x\" y")
            ],
            [2, 0, 1],
        )
        self.assertEqual(
            self.texts('e "a\\"b"'), [("e", False, False), ('a"b', True, False)]
        )

    def test_newline_is_a_separator_and_comments_are_dropped(self):
        toks = self.texts("a\nb")
        self.assertEqual(
            toks, [("a", False, False), ("\n", False, True), ("b", False, False)]
        )
        self.assertEqual(
            self.texts("ls x # > .relays/r/y.md"),
            [("ls", False, False), ("x", False, False)],
        )
        self.assertEqual(
            self.texts("echo '#not a comment'"),
            [("echo", False, False), ("#not a comment", True, False)],
        )
        self.assertEqual(
            len(shell_lex.split_commands(shell_lex.lex("a\nb; c && d"))), 4
        )

    def test_unbalanced_quote_is_none(self):
        self.assertIsNone(shell_lex.lex("echo 'oops"))
