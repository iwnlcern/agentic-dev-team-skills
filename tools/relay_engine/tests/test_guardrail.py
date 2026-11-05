"""Pure effect classification controls for the host guardrail."""

import json
import os
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
        for selector in ("12", "'#12'", "https://github.com/O/R/pull/12"):
            for options in ("--squash", "--body message", "-bmessage", "-t subject",
                            "--body-file file", "-Ffile", "-A a@example.com"):
                command = f"gh pr merge {options} {selector} --match-head-commit {HEAD}"
                match = self.effect(command, "pr-merge")
                self.assertEqual((match.pr, match.head), (12, HEAD), command)
        spoof = self.command(f"gh pr merge 12 --body --match-head-commit={HEAD}")
        self.assertIsNone(spoof.head)
        self.assertNotIn("pinned", spoof.flags)
        for selector in ("", "topic", "²"):
            self.assertIn("unparsed", self.command(f"gh pr merge {selector}").flags)
        self.assertEqual(self.command("GH_REPO=O/R gh pr merge 12").repository, "O/R")
        self.assertEqual(self.command("gh pr merge https://github.com/Other/Repo/pull/12").repository, "Other/Repo")

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
        for mutation in ("mergeBranch", "createCommitOnBranch", "updateRefs", "deleteRef", "enqueuePullRequest"):
            commands.append(f"gh api graphql -f 'query=mutation {{ {mutation}(input: {{}}) {{ clientMutationId }} }}'")
        commands += ["gh api repos/O/R/merge-upstream -f branch=main", "gh extension exec stack merge"]
        for command in commands:
            with self.subTest(command=command):
                self.effect(command, "api-ref-write")
        for command in ("gh api repos/O/R/pulls/12", "gh api repos/O/R/git/refs/heads/main",
                        "gh api -X GET repos/O/R/contents/file", "gh api graphql -f 'query={ viewer { login } }'"):
            self.assertIsNone(self.command(command), command)
        for command in ("gh api repos/O/R/issues/12/comments -f body=repos/O/R/merges",
                        "gh api graphql -f 'query={ viewer { login } }' -f 'body=mergeBranch(input: {})'",
                        "gh api graphql -f 'query=mutation { notupdateRefs(input: {}) }'"):
            self.assertIsNone(self.command(command), command)

    def test_hook_bypass(self):
        for command in ("git push --no-verify origin main", "git commit --no-verify -m x",
                        "git -c core.hooksPath=/tmp commit -m x",
                        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/tmp git push",
                        "env GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/tmp git commit -m x"):
            self.effect(command, "hook-bypass")
        for command in ("git commit -n -m y", "git commit -nm y", "git commit -anm y",
                        "export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/tmp; git commit -m x",
                        "GIT_CONFIG_KEY_0=core.hooksPath; GIT_CONFIG_VALUE_0=/tmp; git push origin main",
                        "git --config-env=core.hooksPath=HOOK_DIR commit -m x",
                        "GIT_CONFIG_PARAMETERS=\"'core.hooksPath'='/tmp'\" git push origin main",
                        "git config core.hooksPath /tmp", "git config --unset core.hooksPath"):
            self.effect(command, "hook-bypass")
        self.effect("git push -n origin HEAD:main", "protected-push")
        for command in ('git commit -m "docs: mention core.hooksPath"',
                        "git log --grep core.hooksPath", "git grep -- --no-verify",
                        "git config --get core.hooksPath", "git config --get-all core.hooksPath",
                        'git commit -m "--no-verify"', "git commit -- --no-verify"):
            self.assertIsNone(self.command(command), command)

    def test_key_file_read(self):
        for path in REFUSED:
            self.assertEqual(g.classify_tool("Read", {"file_path": path}, REFUSED).path, path)
            for program in ("cat", "head", "cp"):
                self.assertEqual(self.effect(f"{program} '{path}' /tmp/out", "key-file-read").path, path)
        self.assertIsNone(g.classify_tool("Read", {"file_path": "/src/a.py"}, REFUSED))
        self.assertIsNone(self.command("cat /src/a.py"))
        home = os.environ["HOME"]
        refused = (home + "/.keys/operator.key",)
        for path in ("~/.keys/operator.key", "$HOME/.keys/operator.key", "${HOME}/.keys/operator.key"):
            for tool in ("Read", "mcp__filesystem__read_file", "mcp__filesystem__read_text_file"):
                self.assertEqual(g.classify_tool(tool, {"path": path}, refused, home=home).path, refused[0])
            for spelling in (path, '"' + path.replace("~/", "${HOME}/") + '"'):
                self.assertEqual(g.classify_tool("Bash", {"command": "cat " + spelling}, refused, home=home).path, refused[0])
        self.assertIsNotNone(self.command("cd /keys && cat operator.key"))
        self.assertIsNone(self.command("cd /keys && cat ordinary.key"))
        for tool, payload in (("Grep", {"path": REFUSED[0], "output_mode": "content"}),
                              ("NotebookRead", {"notebook_path": REFUSED[0]}),
                              ("mcp__filesystem__read_multiple_files", {"paths": ["/src/a", REFUSED[0]]}),
                              ("Grep", {"path": "/keys", "output_mode": "content"}),
                              ("Grep", {"path": "/", "glob": "operator.key", "output_mode": "content"}),
                              ("mcp__filesystem__search", {"directory": "/keys"})):
            self.assertEqual(g.classify_tool(tool, payload, REFUSED).effect, "key-file-read")
        for tool in ("Glob", "mcp__filesystem__list_directory"):
            self.assertIsNone(g.classify_tool(tool, {"path": "/keys"}, REFUSED))
        self.assertIsNone(g.classify_tool("Grep", {"path": "/keys", "output_mode": "files_with_matches"}, REFUSED))
        for path in ("~/.keys/operator.key", "$HOME/.keys/operator.key", "${HOME}/.keys/operator.key"):
            match = g.classify_tool("Read", {"path": path}, refused)
            self.assertEqual(match.effect, "key-file-read")
            self.assertIn("unparsed", match.flags)

    def test_operator_endpoints(self):
        for command in ("tools/relay answer q allow", "relay authority prompt-record --text x",
                        "relay operator answer q deny", "relay prompt record --text x"):
            self.effect(command, "operator-endpoint")
        for command in ("tools/relay engine off", "relay team open next", "relay team open-next"):
            self.effect(command, "operator-only")
        self.assertIsNone(self.command("tools/relay engine restart"))
        for command in ("tools/relay --root /r team open next", "python3 tools/relay --root /r engine off"):
            self.effect(command, "operator-only")
        for command in ("python3 -m relay_engine --root /r authority prompt-record --text x",
                        "relay --root /r operator answer q deny"):
            self.effect(command, "operator-endpoint")
        self.assertIsNone(self.command("relay show answer"))

    def test_final_engine_command_spellings(self):
        for command in ("adt engine stop", "tools/adt engine stop",
                        "/opt/bin/adt engine stop", 'env FOO=1 bash -c "adt engine stop"'):
            with self.subTest(command=command):
                self.assertEqual(self.command(command), g.Match("operator-only"))
        for command in ("adt engine restart", "tools/adt engine restart",
                        "adt show engine stop", "echo adt engine stop"):
            with self.subTest(command=command):
                self.assertIsNone(self.command(command))

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
            self.assertEqual(self.command(command), g.Match("protected-push", ref="main"))
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
        for command in ("gh pr -R O/R merge 12", "gh pr --repo O/R merge 12 --admin",
                        "gh pr --hostname github.com merge 12 --auto",
                        "bash --norc -c 'gh pr merge 12'", "bash --rcfile file -c 'gh pr merge 12'",
                        "bash -c -- 'gh pr merge 12'", "dash -c 'gh pr merge 12'", "ksh -c 'gh pr merge 12'",
                        "command gh pr merge 12", "builtin gh pr merge 12", "exec gh pr merge 12",
                        "nohup gh pr merge 12", "time -p gh pr merge 12", "nice -n 5 gh pr merge 12",
                        "timeout 10 gh pr merge 12", "stdbuf -oL gh pr merge 12", "sudo -u jack gh pr merge 12",
                        "xargs -n 1 gh pr merge 12", "eval 'gh pr merge 12'", "env -S 'gh pr merge 12'",
                        "printf '%s' 'gh pr merge 12' | bash", "echo 'gh pr merge 12' | sh",
                        "bash -s <<< 'gh pr merge 12'", "> /tmp/log gh pr merge 12"):
            with self.subTest(command=command):
                self.effect(command, "pr-merge")
        for command in ("mystery gh pr merge 12", "mystery git push origin HEAD:main",
                        "bash --unknown 'gh pr merge 12'"):
            match = self.command(command)
            self.assertIsNotNone(match, command)
            self.assertIn("unparsed", match.flags, command)
        self.assertIsNone(self.command("timeout 10 gh pr view 12"))
        for command, repository in ((f"gh --repo O/R pr merge 12 --match-head-commit {HEAD}", "O/R"),
                                    ("gh -R O/R pr merge 12", "O/R"),
                                    ("gh --hostname h.example pr merge 12", None)):
            match = self.effect(command, "pr-merge")
            self.assertEqual((match.pr, match.repository), (12, repository))
        for count in (10, 11):
            import shlex
            shell = "gh pr merge 12"
            for _ in range(count):
                shell = "bash -c " + shlex.quote(shell)
            for command in (shell, "env " * count + "gh pr merge 12"):
                match = self.command(command)
                if count == 10:
                    self.assertEqual(match, g.Match("pr-merge", pr=12), command)
                else:
                    self.assertEqual(match.effect, "pr-merge")
                    self.assertIn("unparsed", match.flags)
        for command, effect in (("gh pr merge '12", "pr-merge"), ("git push 'origin", "protected-push")):
            match = self.command(command)
            self.assertEqual(match.effect, effect)
            self.assertIn("unparsed", match.flags)
            self.assertIsNone(match.pr)
            self.assertIsNone(match.ref)
        for command in ("git push origin +main", "git push --repo origin HEAD:main",
                        "git push -o ci.skip origin HEAD:main"):
            match = self.effect(command, "protected-push")
            self.assertEqual(match.ref, "main")
            self.assertTrue(g.protected(match.ref, ["main"]))
        self.assertEqual(g.classify_tool("exec_command", {"cmd": "gh pr merge 12"}, REFUSED),
                         g.Match("pr-merge", pr=12))

    def test_mcp_merge_tool(self):
        for head in (HEAD, None):
            payload = {"owner": "O", "repo": "R", "pullNumber": 12}
            if head:
                payload["expectedHeadSha"] = head
            match = g.classify_tool("mcp__github__merge_pull_request", payload, REFUSED)
            self.assertEqual((match.effect, match.repository, match.pr, match.head), ("pr-merge", "O/R", 12, head))
            self.assertEqual("pinned" in match.flags, head is not None)
        payload = {"owner": "O", "repo": "R", "pull_number": 12, "expectedHeadSha": "bogus"}
        match = g.classify_tool("mcp__github__merge_pull_request", payload, REFUSED)
        self.assertEqual((match.pr, match.head), (12, None))
        self.assertNotIn("pinned", match.flags)

    def test_mcp_branch_writes(self):
        refs = {"*": ["refs/heads/main", "refs/heads/v3"]}
        for prefix in ("mcp__github__", "mcp__plugin_github_github__"):
            for operation in ("push_files", "create_or_update_file", "delete_file"):
                for branch, ref in (("main", "refs/heads/main"), ("v3", "refs/heads/v3"), ("refs/heads/main", "refs/heads/main")):
                    payload = {"owner": "O", "repo": "R", "branch": branch}
                    self.assertEqual(g.classify_tool(prefix + operation, payload, REFUSED, protected_refs=refs),
                                     g.Match("api-ref-write", repository="O/R", ref=ref))
                for branch in (None, 3, ""):
                    payload = {"owner": "O", "repo": "R"}
                    if branch is not None:
                        payload["branch"] = branch
                    match = g.classify_tool(prefix + operation, payload, REFUSED, protected_refs=refs)
                    self.assertEqual(match.effect, "api-ref-write")
                    self.assertIn("unparsed", match.flags)
                self.assertIsNone(g.classify_tool(prefix + operation, {"owner": "O", "repo": "R", "branch": "feature/x"}, REFUSED, protected_refs=refs))
        mapping = {"O/R": ["refs/heads/release"], "*": ["refs/heads/main"]}
        for owner, repo, branch, wanted in (("O", "R", "release", True), ("O", "R", "main", False), ("P", "Q", "main", True)):
            match = g.classify_tool("mcp__github__push_files", {"owner": owner, "repo": repo, "branch": branch}, REFUSED, protected_refs=mapping)
            self.assertEqual(match is not None, wanted)
        self.assertEqual(g.classify_tool("mcp__github__push_files", {"owner": "O", "repo": "R", "branch": "main"}, REFUSED).ref, "refs/heads/main")
        self.assertEqual(g.classify_tool("mcp__github__update_pull_request_branch", {"owner": "O", "repo": "R", "pullNumber": 12}, REFUSED).effect, "api-ref-write")

    def test_path_candidates_are_whole_values(self):
        merge = {"owner": "O", "repo": "R", "pullNumber": 12, "expectedHeadSha": HEAD}
        for cwd in (None, "/repo"):
            self.assertEqual(g.classify_tool("mcp__github__merge_pull_request", merge, REFUSED, cwd=cwd),
                             g.Match("pr-merge", repository="O/R", pr=12, head=HEAD, flags=frozenset({"pinned"})))
            self.assertIsNone(g.classify_tool("mcp__github__push_files", {"owner": "O", "repo": "R", "branch": "feature/x", "files": [{"path": "src/a.py", "content": "hello"}]}, REFUSED, cwd=cwd))
        self.assertIsNone(g.classify_tool("Edit", {"new_string": "prose mentions /keys/operator.key here"}, REFUSED))
        refused = ("/scratch/jev/key",)
        for cwd, flags in ((None, frozenset({"unparsed"})), ("/scratch", frozenset())):
            self.assertEqual(g.classify_tool("Read", {"path": "jev/key"}, refused, cwd=cwd), g.Match("key-file-read", path=refused[0], flags=flags))
        self.assertIsNone(g.classify_tool("Read", {"path": "jev/key"}, refused, cwd="/other"))
        self.assertEqual(g.classify_tool("mcp__github__merge_pull_request", {**merge, "body": REFUSED[0]}, REFUSED).effect, "key-file-read")
        self.assertEqual(g.classify_tool("mcp__github__merge_pull_request", {**merge, "body": "mention " + REFUSED[0]}, REFUSED).effect, "pr-merge")
        self.assertEqual(g.classify_tool("FutureTool", {"unknown_paths": [REFUSED[0]]}, REFUSED).effect, "key-file-read")

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
        self.assertIsNone(self.command("env " * 12 + "gh pr view 12"))

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
        for ref in ("HEAD", "@"):
            match = self.command("git push origin " + ref)
            self.assertIsNone(match.ref)
            self.assertIn("unparsed", match.flags)

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
        new = g.parse_prepush(f"refs/heads/new {HEAD} refs/heads/new {ZERO}\n")[0]
        self.assertEqual((new.remote_sha, new.delete, new.force), (ZERO, False, None))
        mirror = self.effect("git push --mirror origin", "protected-push")
        self.assertIsNone(mirror.ref)
        self.assertIn("unparsed", mirror.flags)
        with self.assertRaises(ValueError):
            g.parse_prepush("refs/heads/a missing-fields")
        self.assertTrue(g.protected("main", ["refs/heads/main"]))
        self.assertTrue(g.protected("refs/heads/main", ["main"]))
        self.assertTrue(g.protected("refs/heads/v3", ["refs/heads/main", "refs/heads/v3"]))
        self.assertFalse(g.protected("refs/tags/main", ["refs/heads/main"]))
        self.assertFalse(g.protected("refs/heads/topic", ["refs/heads/main"]))
        rows = g.parse_prepush(f"refs/remotes/origin/main {HEAD} refs/remotes/origin/main {ZERO}\n")
        self.assertEqual((rows[0].remote_ref, rows[0].delete), ("refs/remotes/origin/main", False))
        rows = g.parse_prepush(f"refs/heads/topic {HEAD} refs/heads/topic {ZERO}\n")
        self.assertFalse(any(g.protected(row.remote_ref, ["main"]) for row in rows))
        self.assertEqual(g.parse_prepush(f"refs/heads/a {'a'*64} refs/heads/a {'0'*64}\n")[0].local_sha, 'a'*64)
        with self.assertRaises(ValueError):
            g.parse_prepush(f"refs/heads/a not-a-sha refs/heads/a {ZERO}")

    def test_review_boundary_controls(self):
        for path in ("/keys/../keys/operator.key", "/keys//operator.key"):
            self.assertEqual(g.classify_tool("Read", {"file_path": path}, REFUSED).path, REFUSED[0])
        self.assertEqual(g.classify_tool("mcp__filesystem__read_file", {"path": REFUSED[0]}, REFUSED).path, REFUSED[0])
        self.assertIsNone(self.command("cat /keys/operator.key.bak"))
        self.assertIsNone(self.command("gh api -X HEAD repos/O/R/git/refs/heads/main"))
        self.effect("tools/relay.py answer q allow", "operator-endpoint")

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
        self.assertEqual(set(table["flags"]["values"]), {"pinned", "auto", "admin", "unparsed", "compound"})
        self.assertEqual(table["flags"]["unresolved_target"], "refuse-or-resolve")
        self.assertEqual(table["operator_commands"],
                         {"stop": [["adt", "engine", "stop"]],
                          "restart": [["adt", "engine", "restart"]]})
        for spelling in table["operator_commands"]["stop"]:
            self.effect(" ".join(spelling), "operator-only")
        for spelling in table["operator_commands"]["restart"]:
            self.assertIsNone(self.command(" ".join(spelling)))
        table["protected_refs"].append("refs/heads/v3")
        self.assertEqual(g.table()["protected_refs"], ["refs/heads/main"])

    def test_ordinary_calls_pass(self):
        for command in ("git status", "git commit -m x", "ls", "echo gh", "gh pr view 12"):
            self.assertIsNone(self.command(command), command)
        self.assertIsNone(g.classify_tool("Edit", {"file_path": "/src/a.py"}, REFUSED))
        for marker in ("EOF", "'EOF'", "-EOF"):
            command = f"cat > f <<{marker}\nIt's a runbook: git push origin HEAD:main\nEOF\n"
            self.assertIsNone(self.command(command), command)
            self.effect(command + "gh pr merge 12", "pr-merge")
        self.assertIsNone(self.command('gh pr comment 52 --body "$(cat <<\'EOF\'\nIt\'s ready.\nEOF\n)"'))
        self.effect("cat <<'EOF'\n/keys/operator.key\nEOF\n", "key-file-read")
        self.assertIsNone(self.command("gh pr comment 'unfinished"))
        self.effect("echo ready # <<EOF\ngh pr merge 12\nEOF\n", "pr-merge")


if __name__ == "__main__":
    unittest.main()
