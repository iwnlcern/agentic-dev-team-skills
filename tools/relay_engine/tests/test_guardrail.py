"""Pure effect classification controls for the host guardrail."""

import json
import unittest

from relay_engine import guardrail as g


HEAD = "a" * 40
ZERO = "0" * 40
REFUSED = ("/keys/operator.key", "/keys/jev.key", "/keys/prompt.key", "/seats/other.key")


class TestGuardrail(unittest.TestCase):
    def command(self, command):
        return g.classify_tool("Bash", {"command": command}, REFUSED)

    def effect(self, command, effect):
        match = self.command(command)
        self.assertIsNotNone(match, command)
        self.assertEqual(match.effect, effect, command)
        return match

    def test_pr_merge_pinned(self):
        match = self.effect(f"gh pr merge 12 --repo O/R --match-head-commit {HEAD}", "pr-merge")
        self.assertEqual((match.repository, match.pr, match.head), ("O/R", 12, HEAD))
        self.assertIn("pinned", match.flags)

    def test_pr_merge_unpinned(self):
        match = self.effect("gh pr merge 12", "pr-merge")
        self.assertEqual(match.pr, 12)
        self.assertIsNone(match.head)
        self.assertNotIn("pinned", match.flags)
        self.assertNotIn("pinned", self.command("gh pr merge 12 --match-head-commit bogus").flags)

    def test_pr_merge_flags(self):
        match = self.effect("gh pr merge 12 --auto --admin", "pr-merge")
        self.assertTrue({"auto", "admin"} <= match.flags)

    def test_api_writes(self):
        commands = [
            "gh api repos/O/R/pulls/12/merge -X PUT",
            "gh api --method PATCH repos/O/R/git/refs/heads/main -f sha=x",
            "gh api repos/O/R/git/refs -f ref=refs/heads/main",
            "gh api -X DELETE repos/O/R/git/refs/heads/main",
            "gh api repos/O/R/contents/file -f message=x -f content=eA==",
            "gh api repos/O/R/merges -f base=main -f head=topic",
            "gh repo sync O/R", "gh stack merge",
        ]
        for mutation in ("mergePullRequest", "updateRef", "enablePullRequestAutoMerge"):
            commands.append(f"gh api graphql -f 'query=mutation {{ {mutation}(input: {{}}) {{ clientMutationId }} }}'")
        for command in commands:
            with self.subTest(command=command):
                self.effect(command, "api-ref-write")
        for command in ("gh api repos/O/R/pulls/12", "gh api repos/O/R/git/refs/heads/main",
                        "gh api -X GET repos/O/R/contents/file", "gh api graphql -f 'query={ viewer { login } }'"):
            self.assertIsNone(self.command(command), command)

    def test_hook_bypass(self):
        for command in ("git push --no-verify origin main", "git commit --no-verify -m x",
                        "git -c core.hooksPath=/tmp commit -m x",
                        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/tmp git push",
                        "env GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/tmp git commit -m x"):
            self.effect(command, "hook-bypass")

    def test_key_file_read(self):
        for path in REFUSED:
            self.assertEqual(g.classify_tool("Read", {"file_path": path}, REFUSED).path, path)
            for program in ("cat", "head", "cp"):
                self.assertEqual(self.effect(f"{program} '{path}' /tmp/out", "key-file-read").path, path)
        self.assertIsNone(g.classify_tool("Read", {"file_path": "/src/a.py"}, REFUSED))
        self.assertIsNone(self.command("cat /src/a.py"))

    def test_operator_endpoints(self):
        for command in ("tools/relay answer q allow", "relay authority prompt-record --text x",
                        "relay operator answer q deny", "relay prompt record --text x"):
            self.effect(command, "operator-endpoint")
        for command in ("tools/relay engine off", "relay team open next", "relay team open-next"):
            self.effect(command, "operator-only")
        self.assertIsNone(self.command("tools/relay engine restart"))

    def test_command_spellings(self):
        plain = self.command(f"gh pr merge 12 --match-head-commit {HEAD}")
        for command in (f"gh  pr   merge 12 --match-head-commit {HEAD}",
                        f"'gh' 'pr' 'merge' '12' --match-head-commit={HEAD}",
                        f"FOO=1 gh pr merge 12 --match-head-commit {HEAD}",
                        f"env gh pr merge 12 --match-head-commit {HEAD}",
                        f"env -u FOO BAR=1 gh pr merge 12 --match-head-commit {HEAD}",
                        f"bash -lc 'gh pr merge 12 --match-head-commit {HEAD}'"):
            self.assertEqual(self.command(command), plain, command)
        for command in ("git push origin HEAD:main", "git -C x push origin HEAD:main",
                        "FOO=1 git push origin HEAD:main", "sh -c 'git push origin HEAD:main'"):
            self.effect(command, "protected-push")
        for command in ("gh pr merge '12", "git push 'origin", "cat '/keys/operator.key"):
            self.assertIsNotNone(self.command(command), command)
        nested = "gh pr merge 12"
        import shlex
        for _ in range(12):
            nested = "bash -c " + shlex.quote(nested)
        self.assertIsNotNone(self.command(nested))
        for command in ("echo ready && gh pr merge 12", "git status; gh pr merge 12",
                        "FOO=$(gh pr merge 12) echo x"):
            self.effect(command, "pr-merge")

    def test_mcp_merge_tool(self):
        for head in (HEAD, None):
            payload = {"owner": "O", "repo": "R", "pullNumber": 12}
            if head:
                payload["expectedHeadSha"] = head
            match = g.classify_tool("mcp__github__merge_pull_request", payload, REFUSED)
            self.assertEqual((match.effect, match.repository, match.pr, match.head), ("pr-merge", "O/R", 12, head))
            self.assertEqual("pinned" in match.flags, head is not None)

    def test_compound_effects(self):
        self.effect("git status\ngh pr merge 12", "pr-merge")
        self.effect(f"gh pr merge 12 --match-head-commit {HEAD}; gh api repos/O/R/merges -f head=x", "api-ref-write")
        match = self.command(f"gh pr merge 12 --match-head-commit {HEAD}; gh pr merge 13 --match-head-commit {HEAD}")
        self.assertEqual(match.effect, "pr-merge")
        self.assertNotIn("pinned", match.flags)

    def test_effective_option_values(self):
        for method in ("-XPUT", "-XDELETE"):
            self.effect(f"gh api {method} repos/O/R/git/refs/heads/main", "api-ref-write")
        for flag in ("auto", "admin"):
            match = self.command(f"gh pr merge 12 --match-head-commit {HEAD} --{flag}=true")
            self.assertIn(flag, match.flags)
        for options in ("-ROther/Repo", "--repo O/R --repo Other/Repo"):
            self.assertEqual(self.command(f"gh pr merge 12 {options}").repository, "Other/Repo")
        other = "b" * 40
        self.assertEqual(self.command(f"gh pr merge 12 --match-head-commit {HEAD} --match-head-commit={other}").head, other)
        self.assertNotIn("pinned", self.command(f"gh pr merge 12 --match-head-commit {HEAD} --match-head-commit invalid").flags)
        self.assertIsNone(self.command("gh api -XGET repos/O/R/git/refs/heads/main"))

    def test_comment_cannot_pin_merge(self):
        match = self.command(f"gh pr merge 12 # --match-head-commit {HEAD}")
        self.assertIsNone(match.head)
        self.assertNotIn("pinned", match.flags)
        self.assertEqual(self.command(f"gh pr merge 12 --match-head-commit {HEAD} # note").head, HEAD)
        self.assertIsNone(self.command("echo ready # gh pr merge 12"))

    def test_shell_constructs(self):
        for command in ("if true; then gh pr merge 12; fi", "{ gh pr merge 12; }",
                        "if gh pr merge 12; then echo done; fi", "! gh pr merge 12",
                        'echo "$(gh pr merge 12)"', "echo `gh pr merge 12`",
                        'echo "$(echo x; gh pr merge 12)"'):
            with self.subTest(command=command):
                self.effect(command, "pr-merge")
        for command in ("echo '$(gh pr merge 12)'", "echo '`gh pr merge 12`'"):
            self.assertIsNone(self.command(command), command)

    def test_wrapper_composition_and_context(self):
        for command in ("FOO=1 env gh pr merge 12", "env FOO=1 env gh pr merge 12"):
            self.effect(command, "pr-merge")
        self.effect('env GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath '
                    'GIT_CONFIG_VALUE_0=/tmp bash -c "git commit -m x"', "hook-bypass")
        self.assertIsNone(self.command('env FOO=1 bash -c "git status"'))
        self.assertIsNotNone(self.command("env " * 12 + "gh pr view 12"))

    def test_literal_key_path_spellings(self):
        for path in ('/keys/"operator.key"', '/keys/../keys/operator.key'):
            self.assertEqual(self.effect(f"cat {path}", "key-file-read").path, REFUSED[0])
        self.assertIsNone(self.command('cat /keys/"ordinary.key"'))

    def test_multiple_push_targets(self):
        for command in ("git push origin HEAD:main HEAD:topic",
                        "git push origin HEAD:topic; git push origin HEAD:main"):
            match = self.effect(command, "protected-push")
            self.assertIsNone(match.ref)
            self.assertIn("unparsed", match.flags)
        self.assertEqual(self.command("git push origin HEAD:topic").ref, "topic")

    def test_parse_prepush(self):
        rows = g.parse_prepush(f"refs/heads/a {HEAD} refs/heads/main {'b'*40}\n"
                              f"refs/heads/a {'c'*40} refs/heads/v3 {HEAD}\n"
                              f"(delete) {ZERO} refs/heads/old {HEAD}\n"
                              f"refs/tags/v1 {HEAD} refs/tags/v1 {ZERO}\n")
        self.assertEqual(len(rows), 4)
        self.assertEqual((rows[0].remote_ref, rows[0].local_sha), ("refs/heads/main", HEAD))
        self.assertTrue(all(row.force is None for row in rows))
        self.assertEqual([row.delete for row in rows], [False, False, True, False])
        self.assertEqual(g.parse_prepush(""), [])
        with self.assertRaises(ValueError):
            g.parse_prepush("refs/heads/a missing-fields")
        self.assertTrue(g.protected("main", ["refs/heads/main"]))
        self.assertTrue(g.protected("refs/heads/v3", ["refs/heads/main", "refs/heads/v3"]))
        self.assertFalse(g.protected("refs/tags/main", ["refs/heads/main"]))
        self.assertFalse(g.protected("refs/heads/topic", ["refs/heads/main"]))

    def test_agent_environment(self):
        for env in ({"CLAUDECODE": "1"}, {"CODEX_THREAD_ID": ""}):
            self.assertTrue(g.is_agent_env(env))
        self.assertFalse(g.is_agent_env({"OTHER": "1"}))

    def test_table_deterministic(self):
        table = g.table()
        self.assertEqual(json.dumps(table, sort_keys=True), json.dumps(g.table(), sort_keys=True))
        self.assertEqual(table["protected_refs"], ["refs/heads/main"])
        self.assertEqual(set(table["effects"]), set(g.EFFECT_CLASSES))
        self.assertEqual(table["effects"]["commit"]["slip_kind"], "implement")
        self.assertEqual(table["effects"]["pr-merge"]["slip_kind"], "merge")
        table["protected_refs"].append("refs/heads/v3")
        self.assertEqual(g.table()["protected_refs"], ["refs/heads/main"])

    def test_ordinary_calls_pass(self):
        for command in ("git status", "git commit -m x", "ls", "echo gh", "gh pr view 12"):
            self.assertIsNone(self.command(command), command)
        self.assertIsNone(g.classify_tool("Edit", {"file_path": "/src/a.py"}, REFUSED))


if __name__ == "__main__":
    unittest.main()
