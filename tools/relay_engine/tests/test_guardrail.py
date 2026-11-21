"""Pure effect classification controls for the host guardrail."""

import ast
import json
import os
import shlex
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from relay_engine import api_operator_bridge, lifecycle_paths
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
                        "GIT_CONFIG_PARAMETERS=\"'core.hooksPath'='/tmp'\" git push origin main"):
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

    def test_configure_shape_opaque_and_malformed(self):
        for tail in ("", " --jev-key-fd", " --jev-key-fd 3", " --jev-key-fd3",
                     " --jev-key-fd 0", " --jev-key-fd not-a-number",
                     " --jev-key-fd 3 --jev-key-fd 4", " --unknown option",
                     " --jev-key-fd 3 --store-root /fake/existing-store"):
            with self.subTest(tail=tail):
                self.assertEqual(self.command("adt engine configure" + tail), g.Match("operator-only"))

    def test_configure_normalization(self):
        command = "adt engine configure --jev-key-fd 3"
        commands = ["tools/" + command, "/opt/bin/" + command,
                    "'adt' 'engine' 'configure' --jev-key-fd 3",
                    "FOO=1 " + command, "env -u FOO BAR=1 " + command]
        for wrapper in ("command", "builtin", "exec", "nohup", "time -p", "nice -n 5",
                        "timeout 10", "stdbuf -oL", "sudo -u fake", "xargs -n 1"):
            commands.append(wrapper + " " + command)
        for shell in ("bash", "sh", "zsh", "dash", "ksh"):
            commands.append(shell + " -c " + shlex.quote(command))
        commands += ["env " * 10 + command, "env -S " + shlex.quote(command),
                     "echo " + shlex.quote(command) + " | sh",
                     "printf '%s' " + shlex.quote(command) + " | bash",
                     "echo $(" + command + ")", "true && " + command,
                     f"gh pr merge 12 --match-head-commit {HEAD}; " + command]
        commands += ['rg "$(' + command + ')" docs/',
                     'grep -F "`' + command + '`" README.md']
        for value in commands:
            with self.subTest(command=value):
                self.assertEqual(self.command(value), g.Match("operator-only"))
        for field in ("command", "cmd"):
            self.assertEqual(g.classify_tool("Bash", {field: command}, REFUSED), g.Match("operator-only"))

    def test_configure_named_fallback_closed(self):
        command = "adt engine configure --jev-key-fd 3"
        nested = command
        for _ in range(12):
            nested = "bash -c " + shlex.quote(nested)
        for value in (command + " '", "adt 'engine' configure '3",
                      "mystery " + command, "mystery /opt/bin/" + command,
                      "env " * 11 + command, nested,
                      "gh pr merge '12; " + command,
                      "rg -n 'adt engine configure",
                      "env " * 11 + "rg -n 'adt engine configure' docs/",
                      "mystery rg -n 'adt engine configure' docs/",
                      "rg --pre 'adt engine configure' -e value docs/",
                      "rg --pre='adt engine configure' value docs/",
                      "rg --pre helper -e 'adt engine configure' docs/",
                      "rg --hostname-bin 'adt engine configure' value docs/"):
            with self.subTest(command=value):
                self.assertEqual(self.command(value), g.Match("operator-only", flags=frozenset({"unparsed"})))

    def test_configure_data_restart_positive(self):
        for value in ("echo 'adt engine configure --jev-key-fd 3'",
                      "printf '%s' 'adt engine configure'", "echo ready # adt engine configure",
                      "cat <<'EOF'\nIt's a runbook: adt engine configure --jev-key-fd 3\nEOF\n",
                      "echo '$(adt engine configure)'", "cat <&3", "echo ready 3>/fake/output",
                      "adt engine restart", "tools/adt engine restart", "adt engine status",
                      "adt show engine configure", "adt engine configure-extra",
                      "mystery not-adt engine configure", "mystery adt engine configure-extra"):
            with self.subTest(command=value):
                self.assertIsNone(self.command(value))
        searches = ("rg -n 'adt engine configure' docs/",
                    "grep -F 'adt engine configure' README.md",
                    "rg -e 'adt engine configure' docs/",
                    "grep -e 'adt engine configure' README.md",
                    "rg -nFe'adt engine configure' docs/",
                    "grep --regexp='adt engine configure' README.md",
                    "rg -n -- 'adt engine configure' docs/",
                    "rg -e --pre 'adt engine configure'",
                    "rg '$(adt engine configure)' docs/",
                    "grep -F '`adt engine configure`' README.md")
        for value in searches:
            for prefix in ("", "/opt/bin/", "env FOO=1 ", "command ", "sudo -u fake "):
                with self.subTest(command=prefix + value):
                    self.assertIsNone(self.command(prefix + value))
        self.assertIsNone(self.command("env " * 10 + searches[0]))

    def test_configure_list_boundary(self):
        for payload in ({"command": ["adt", "engine", "configure", "--jev-key-fd", "3"]},
                        {"cmd": ["adt engine configure"]}, {"examples": ["adt engine configure"]}):
            with self.subTest(payload=payload):
                self.assertIsNone(g.classify_tool("Bash", payload, REFUSED))
        for payload in ({"examples": ["adt engine configure", REFUSED[1]]},
                        {"command": ["adt", "engine", "configure", REFUSED[1]]}):
            self.assertEqual(g.classify_tool("Bash", payload, REFUSED), g.Match("key-file-read", path=REFUSED[1]))

    def test_configure_precedence_and_purity(self):
        self.assertEqual(self.command("adt engine configure " + REFUSED[1]),
                         g.Match("key-file-read", path=REFUSED[1]))
        for value in ("rg -n 'adt engine configure' " + REFUSED[1],
                      "grep -F 'adt engine configure' " + REFUSED[1]):
            self.assertEqual(self.command(value), g.Match("key-file-read", path=REFUSED[1]))
        for value, effect in (("rg -e 'git push' docs/", "protected-push"),
                              ("grep -F 'gh pr merge 12' README.md", "pr-merge"),
                              ("rg -n 'adt engine configure; gh pr merge 12' docs/", "pr-merge"),
                              ("grep -F 'adt engine configure git push' README.md", "protected-push")):
            self.assertEqual(self.command(value), g.Match(effect, flags=frozenset({"unparsed"})))

        class NoEnvironment(dict):
            def forbidden(self, *args, **kwargs):
                raise AssertionError("classifier read environment")
            __getitem__ = __contains__ = __iter__ = __len__ = get = copy = forbidden

        commands = ("adt engine configure --jev-key-fd 3",
                    "env FOO=1 /opt/bin/adt engine configure --store-root /fake/store",
                    "adt engine configure '", "rg -n 'adt engine configure' docs/")
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(os, "environ", NoEnvironment()))
            for target in ("builtins.open", "io.open", "os.open", "os.fdopen", "os.read",
                           "os.write", "os.close", "os.stat", "os.lstat", "os.fstat", "os.getenv"):
                stack.enter_context(mock.patch(target, side_effect=AssertionError("classifier performed I/O")))
            matches = [g.classify_tool("Bash", {"command": value}, REFUSED,
                                       home="/fake/home", cwd="/fake/cwd") for value in commands]
        self.assertEqual(matches, [g.Match("operator-only"), g.Match("operator-only"),
                                  g.Match("operator-only", flags=frozenset({"unparsed"})), None])

    def test_configure_actor_separation(self):
        for actor in ("operator", "seat", "top-seat", "service"):
            payload = {"command": "adt engine configure --jev-key-fd 3", "actor": actor,
                       "operator": True, "credential_kind": actor}
            with self.subTest(actor=actor):
                self.assertEqual(g.classify_tool("Bash", payload, REFUSED), g.Match("operator-only"))

    def test_configure_table_and_regression(self):
        table = g.table()
        self.assertEqual(table["operator_commands"].get("configure"), [["adt", "engine", "configure"]])
        self.assertIn("adt engine configure", table["effects"]["operator-only"]["command_shapes"])
        self.assertIsNone(table["effects"]["operator-only"]["slip_kind"])
        table["operator_commands"]["configure"][0][0] = "changed"
        table["effects"]["operator-only"]["command_shapes"].clear()
        fresh = g.table()
        self.assertEqual(fresh["operator_commands"]["configure"], [["adt", "engine", "configure"]])
        self.assertIn("adt engine configure", fresh["effects"]["operator-only"]["command_shapes"])
        self.assertEqual(self.command("adt engine stop"), g.Match("operator-only"))
        self.assertIsNone(self.command("adt engine restart"))

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
                          "restart": [["adt", "engine", "restart"]],
                          "configure": [["adt", "engine", "configure"]],
                          "start": [["adt", "engine", "start"]],
                          "update": [["adt", "engine", "update"]],
                          "install": [["adt", "engine", "install"]],
                          "checkout_register": [["adt", "checkout", "register"]],
                          "checkout_unregister": [["adt", "checkout", "unregister"]],
                          "unseat": [["adt", "seat", "unseat"]],
                          "seat_claim": [["adt", "seat", "claim"]]})
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


class TestRulesR22(unittest.TestCase):
    operator_commands = ('adt engine stop', 'adt engine stop --project p',
                         'adt engine start', 'adt engine start --project p',
                         'adt engine update', 'adt engine install',
                         'adt checkout register --project p --repository r',
                         'adt checkout unregister', 'adt seat unseat s',
                         'adt engine configure x', 'relay team open', 'relay team open-next')

    def context(self, **values):
        defaults = dict(hooks_dir=None, git_config_paths=(), host_config_paths=(),
                        kit_hook_files=(), kit_entry_files={}, resolve=lambda path: path)
        return g.ClassifierContext(**(defaults | values))

    def classify(self, text, context=None, **kwargs):
        if context is not None:
            kwargs['context'] = context
        return g.classify_tool('Bash', {'command': text}, REFUSED,
                               cwd=kwargs.pop('cwd', '/r'), home=kwargs.pop('home', '/home/u'),
                               **kwargs)

    def file(self, path, content, context, tool='Write', **kwargs):
        if context is not None:
            kwargs['context'] = context
        payload = {'file_path': path, 'content': content} if tool == 'Write' else {'file_path': path, **content}
        return g.classify_tool(tool, payload, REFUSED, home='/home/u', cwd='/r', **kwargs)

    def assert_disable(self, match):
        self.assertIsNotNone(match)
        self.assertEqual(match.effect, 'hook-disable')

    def settings(self, command='"$HOME/.adt/bin/adt" hook PreToolUse --host claude-code'):
        return {'model': 'old', 'hooks': {
            'PreToolUse': [{'matcher': 'Bash|Edit|Write|MultiEdit', 'hooks': [{'command': command}]}],
            'UserPromptSubmit': [{'hooks': [{'command': '"$HOME/.adt/bin/adt" hook UserPromptSubmit --host claude-code'}]}]}}

    def test_table_r22(self):
        table = g.table()
        self.assertEqual(table['design'], 'DD-v3-authority-20261104-r22')
        codes = {'commit': [], 'protected-push': ['protected-ref-push'],
                 'pr-merge': ['merge-head-unpinned', 'merge-flag-refused'],
                 'api-ref-write': ['api-merge-refused'], 'hook-bypass': ['hook-bypass'],
                 'hook-disable': ['hook-disable'], 'key-file-read': ['key-file-read'],
                 'operator-endpoint': ['operator-endpoint'], 'operator-only': ['operator-only'],
                 'top-seat-init': ['operator-only']}
        self.assertEqual(set(table['effects']), set(g.EFFECT_CLASSES))
        self.assertEqual(set(table['effects']), set(codes))
        for effect, names in codes.items():
            with self.subTest(effect=effect):
                self.assertEqual(table['effects'][effect]['hook_codes'], ['authority.' + n for n in names])
                self.assertEqual(table['effects'][effect]['no_seat'],
                                 'refuse' if effect in ('hook-disable', 'key-file-read', 'operator-endpoint', 'operator-only') else 'pass')
        self.assertEqual(table['effects']['commit']['command_shapes'], ['git reference-transaction (prepared)'])
        self.assertEqual(table['effects']['top-seat-init']['command_shapes'], ['adt seat claim'])
        self.assertEqual(table['effects']['operator-only']['command_shapes'],
                         ['adt engine stop', 'adt engine configure', 'relay engine off', 'relay team open',
                          'adt engine start', 'adt engine update', 'adt engine install',
                          'adt checkout register', 'adt checkout unregister', 'adt seat unseat'])
        expected = {name: [text.split()] for name, text in {
            'stop': 'adt engine stop', 'restart': 'adt engine restart', 'configure': 'adt engine configure',
            'start': 'adt engine start', 'update': 'adt engine update', 'install': 'adt engine install',
            'checkout_register': 'adt checkout register', 'checkout_unregister': 'adt checkout unregister',
            'unseat': 'adt seat unseat', 'seat_claim': 'adt seat claim'}.items()}
        self.assertEqual(table['operator_commands'], expected)

    def test_operator_only_commands_r22(self):
        for text in self.operator_commands:
            with self.subTest(text=text):
                self.assertEqual(self.classify(text), g.Match('operator-only'))
        self.assertIsNone(self.classify('adt engine restart'))

    def test_instructed_operator_only_rows_denied(self):
        for text in ('adt engine stop', 'adt engine stop --project p', 'adt engine start --project p',
                     'adt checkout register', 'adt checkout unregister', 'adt seat unseat s'):
            with self.subTest(text=text):
                self.assertEqual(self.classify(text + ' --instruction R1'), g.Match('operator-only'))
        self.assertEqual(g.table()['effects']['operator-only']['no_seat'], 'refuse')

    def test_seat_claim_matches_top_seat_init(self):
        for suffix in ('', ' --parent u'):
            self.assertEqual(self.classify('adt seat claim planner --project p --team t' + suffix), g.Match('top-seat-init'))
        self.assertEqual(self.classify('adt seat claim planner --project p --team t; adt engine stop'), g.Match('operator-only'))
        self.assertEqual(self.classify('relay team open'), g.Match('operator-only'))

    def test_compound_refusal_wins(self):
        for text in ('git push --no-verify; git config core.hooksPath /tmp/x',
                     'git config core.hooksPath /tmp/x; git push --no-verify'):
            self.assertEqual(self.classify(text), g.Match('hook-disable'))
        self.assertEqual(self.classify('git commit --no-verify && cat ' + REFUSED[0]).effect, 'key-file-read')
        self.assertEqual(self.classify('git push --no-verify; adt engine stop'), g.Match('operator-only'))
        self.assertEqual(self.classify('git push --no-verify'), g.Match('hook-bypass'))
        self.assertEqual(self.classify('gh pr merge 12; gh pr merge 13'),
                         g.Match('pr-merge', flags=frozenset({'compound', 'unparsed'})))

    def test_persistent_hooks_path_is_disable(self):
        for text in ('git config core.hooksPath /tmp/x', 'git config --global core.hooksPath x',
                     'git config --unset core.hooksPath', 'git config --file .git/config core.hooksPath x'):
            with self.subTest(text=text): self.assertEqual(self.classify(text), g.Match('hook-disable'))

    def test_include_path_is_disable(self):
        for text in ('git config include.path x', 'git config includeIf.gitdir:/a/.path x'):
            self.assertEqual(self.classify(text), g.Match('hook-disable'))

    def test_git_config_file_writes_are_disable(self):
        paths = ('/r/.git/config', '/r/.git/worktrees/w/config.worktree', '/home/u/.gitconfig', '/nondefault/git/config')
        context = self.context(git_config_paths=paths)
        for path in paths:
            for text in ('[core]\n hooksPath = x\n', '[include]\n path = x\n', '[includeIf "gitdir:/a/"]\n path = x\n'):
                with self.subTest(path=path, text=text):
                    self.assert_disable(self.file(path, text, context))
                    self.assert_disable(self.file(path, {'old_string': '', 'new_string': text}, context, tool='Edit'))
            self.assertIsNone(self.file(path, {'old_string': 'a', 'new_string': 'b'}, context, tool='Edit'))
        self.assertIsNone(self.file('/other/config', '[core]\n hooksPath = x', context))

    def test_hooks_dir_writes_are_disable(self):
        for directory in ('/r/.git/hooks', '/r/custom-hooks'):
            context = self.context(hooks_dir=directory)
            for text in (f'rm {directory}/pre-push', f'mv {directory}/pre-push /tmp/x',
                         f'chmod -x {directory}/pre-push', f': > {directory}/pre-push',
                         'adt hooks remove --checkout /r', f'rm -r {directory}',
                         f'mv {directory} /tmp/x', f'ln -sfn /tmp/x {directory}'):
                with self.subTest(text=text): self.assert_disable(self.classify(text, context))
            self.assert_disable(self.file(directory + '/pre-push', '', context))
            self.assertIsNone(self.classify('cat ' + directory + '/pre-push', context))

    def test_host_hooks_off_is_disable(self):
        claude = ('/nondefault/claude/settings.json', '/r/.claude/settings.local.json')
        codex = ('/nondefault/codex/config.toml', '/nondefault/codex/work.config.toml', '/parent/.codex/config.toml')
        context = self.context(host_config_paths=claude + codex, kit_hook_files=('/kit/hooks.py',))
        for path in claude:
            for text in ('{"disableAllHooks": true}', '{"enabledPlugins": {"adt-orchestrator@agentic-dev-team-skills": false}}'):
                with self.subTest(path=path, text=text): self.assert_disable(self.file(path, text, context))
        for path in codex: self.assert_disable(self.file(path, '[features]\nhooks = false\n', context))
        self.assert_disable(self.file('/kit/hooks.py', 'anything', context))

    def test_kit_entry_removal_is_disable(self):
        settings = self.settings(); original = json.dumps(settings); path = '/r/.claude/settings.json'
        context = self.context(kit_entry_files={path: original}, kit_hook_files=('/kit/hooks.py',))
        kept = self.settings(); del kept['hooks']['PreToolUse']
        self.assert_disable(self.file(path, json.dumps(kept), context))
        for old, new in [('Bash|Edit|Write|MultiEdit', 'Read'), ('PreToolUse', 'PostToolUse')]:
            self.assert_disable(self.file(path, {'old_string': old, 'new_string': new}, context, tool='Edit'))
        self.assert_disable(self.file(path, {'edits': [
            {'old_string': 'old', 'new_string': 'new'},
            {'old_string': '"$HOME/.adt/bin/adt"'.replace('"', '\\"'), 'new_string': '/opt/other', 'replace_all': True}]}, context, tool='MultiEdit'))
        self.assert_disable(self.file(path, 'not JSON', context))
        for target in (path, '/kit/hooks.py'):
            for text in (f'rm {target}', f'mv {target} /tmp/x', f'unlink {target}', f'truncate -s 0 {target}', f': > {target}'):
                with self.subTest(text=text): self.assert_disable(self.classify(text, context))
        settings['model'] = 'new'
        self.assertIsNone(self.file(path, json.dumps(settings), context))
        self.assertIsNone(self.file(path, {'old_string': 'old', 'new_string': 'new'}, context, tool='Edit'))
        self.assertIsNone(self.file(path, {'old_string': 'absent', 'new_string': 'not JSON'}, context, tool='Edit'))
        for text in (f'cat {path}', f'jq . {path}'):
            self.assertIsNone(self.classify(text, context))

    def test_kit_entries_parse(self):
        settings = self.settings(); text = json.dumps(settings)
        expected = frozenset((event, row.get('matcher', ''), hook['command'])
                             for event, rows in settings['hooks'].items() for row in rows for hook in row['hooks'])
        self.assertEqual(g.kit_entries(text, '/home/u'), expected)
        self.assertEqual(g.kit_entries('{}', None), frozenset())
        for text in ('[]', 'not JSON', 'null'):
            self.assertIsNone(g.kit_entries(text, None))
        for launcher in ('"$HOME/.adt/bin/adt"', '$HOME/.adt/bin/adt', '~/.adt/bin/adt',
                         '"${HOME}/.adt/bin/adt"', '"/home/x y/.adt/bin/adt"'):
            text = json.dumps(self.settings(launcher + ' hook PreToolUse'))
            self.assertEqual(len(g.kit_entries(text, '/home/x y')), 2, launcher)
        for command in ('/opt/adt/bin/adt hook x', '"$HOME/.adt/bin/adt" act x', '"unbalanced'):
            self.assertEqual(len(g.kit_entries(json.dumps(self.settings(command)), '/home/u')), 1)
        self.assertEqual(len(g.kit_entries(json.dumps(self.settings('"/home/x y/.adt/bin/adt" hook x')), None)), 1)

    def test_apply_patch_disable(self):
        context = self.context(hooks_dir='/r/.git/hooks', host_config_paths=('/custom/config.toml',),
                               git_config_paths=('/r/.git/config',), kit_hook_files=('/kit/hooks.py',))
        def patch(path, body='', verb='Update File'):
            return f'*** Begin Patch\n*** {verb}: {path}\n@@\n{body}\n*** End Patch\n'
        patches = (patch('/custom/config.toml', '+[features]\n+hooks = false'),
                   patch('.git/hooks/pre-push', '+changed'), patch('/kit/hooks.py', verb='Delete File'),
                   patch('.git/config', '+[core]\n+hooksPath = /tmp/x'))
        for text in patches:
            self.assert_disable(g.classify_tool('apply_patch', {'command': text}, REFUSED, cwd='/r', context=context))
        self.assert_disable(g.classify_tool('exec_command', {'cmd': "apply_patch <<'EOF'\n" + patches[0] + 'EOF\n'}, REFUSED, cwd='/r', context=context))
        self.assert_disable(g.classify_tool('apply_patch', {'command': patches[1]}, REFUSED, context=context))
        for text in (patch('src/app.py', '+print(1)'), patch('/custom/config.toml', '+model = "x"'),
                     patch('src/app.py', '+git push --no-verify')):
            self.assertIsNone(g.classify_tool('apply_patch', {'command': text}, REFUSED, cwd='/r', context=context))

    def test_linked_targets_are_resolved(self):
        paths = {'/home/u/.claude/settings.json': '/home/u/dotfiles/claude-settings.json', '/r/.git/config': '/r/real/config'}
        context = self.context(host_config_paths=('/home/u/dotfiles/claude-settings.json',),
                               git_config_paths=('/r/real/config',), resolve=lambda path: paths.get(path, path))
        self.assert_disable(self.file('~/.claude/settings.json', '{"disableAllHooks": true}', context))
        command = "printf '[core]\\n\\thooksPath = x\\n' >> /r/.git/config"
        self.assert_disable(self.classify(command, context))
        plain = self.context(host_config_paths=context.host_config_paths, git_config_paths=context.git_config_paths)
        self.assertIsNone(self.file('~/.claude/settings.json', '{"disableAllHooks": true}', plain))
        self.assertIsNone(self.classify(command, plain))

    def test_missing_context(self):
        self.assertIsNone(self.classify('rm /r/.git/hooks/pre-push'))
        self.assertIsNone(self.file('/r/.git/config', '[core]\n hooksPath = x', None))
        context = self.context(hooks_dir='/r/.git/hooks')
        self.assert_disable(self.classify('rm .git/hooks/pre-push', context, cwd=None))

    def test_config_reads_pass(self):
        for text in ('git config --get core.hooksPath', 'git config --get-all core.hooksPath',
                     'git config --list', 'git config -l', 'git config --show-origin core.hooksPath',
                     'git config core.hooksPath'):
            self.assertIsNone(self.classify(text), text)

    def test_one_command_stays_bypass(self):
        self.assertEqual(self.classify('git -c core.hooksPath=x commit'), g.Match('hook-bypass'))
        self.assertEqual(self.classify('GIT_CONFIG_KEY_0=core.hooksPath git commit'), g.Match('hook-bypass'))

    def test_settings_edit_keeping_hooks_passes(self):
        path = '/r/.claude/settings.json'; context = self.context(host_config_paths=(path,))
        self.assertIsNone(self.file(path, {'old_string': '"model": "old"', 'new_string': '"model": "new"'}, context, tool='Edit'))
        self.assertIsNone(self.file(path, '{"disableAllHooks": false, "enabledPlugins": {"adt-pair": true}}', context))

    def test_key_spellings_match(self):
        for text in ('git config CORE.HOOKSPATH x', "git config 'core.hooksPath' x", 'git config --file .git/config core.hooksPath x'):
            self.assertEqual(self.classify(text), g.Match('hook-disable'))

    def test_f1_symbolic_hook_mutations(self):
        context = self.context(hooks_dir='/r/custom-hooks')
        for command in ('chmod u-x /r/custom-hooks/pre-push',
                        'chmod g-x /r/custom-hooks/pre-push',
                        'chmod a-x /r/custom-hooks/pre-push',
                        'cp /dev/null /r/custom-hooks/pre-push'):
            with self.subTest(command=command):
                self.assertEqual(self.classify(command, context), g.Match('hook-disable'))
        self.assertIsNone(self.classify('cat /r/custom-hooks/pre-push', context))
        linked = self.context(hooks_dir='/r/custom-hooks',
                              resolve=lambda path: '/r/custom-hooks/pre-push' if path == '/r/linked-hook' else path)
        self.assertEqual(self.classify('chmod u-x linked-hook', linked), g.Match('hook-disable'))
        self.assertEqual(self.classify('cp /dev/null linked-hook', linked), g.Match('hook-disable'))

    def test_f2_valid_plugin_edit_fragment(self):
        path = '/nondefault/claude/settings.json'
        context = self.context(host_config_paths=(path,))
        enabled = '{"adt-orchestrator@agentic-dev-team-skills": true}'
        disabled = '{"adt-orchestrator@agentic-dev-team-skills": false}'
        self.assertEqual(self.file(path, {'old_string': enabled, 'new_string': disabled}, context, tool='Edit'),
                         g.Match('hook-disable'))
        self.assertIsNone(self.file(path, {'old_string': disabled, 'new_string': enabled}, context, tool='Edit'))
        self.assertIsNone(self.file(path, {'old_string': '{"model": "a"}', 'new_string': '{"model": "b"}'}, context, tool='Edit'))

    def test_f3_absolute_git_fallback(self):
        command = r'find . -prune -exec /usr/bin/git push origin main \;'
        self.assertEqual(self.classify(command),
                         g.Match('protected-push', flags=frozenset({'unparsed'})))
        self.assertIsNone(self.classify('rm /r/.git/hooks/pre-push'))

    def test_f4_patch_compound_order(self):
        context = self.context(kit_hook_files=('/kit/hooks.py',))
        patch = "apply_patch <<'EOF'\n*** Begin Patch\n*** Delete File: /kit/hooks.py\n*** End Patch\nEOF\n"
        self.assertEqual(self.classify('adt engine stop; ' + patch, context), g.Match('operator-only'))
        self.assertEqual(self.classify(patch, context), g.Match('hook-disable'))
        self.assertEqual(self.classify(patch + 'adt engine stop', context), g.Match('hook-disable'))

    def test_f4_substitution_retains_patch_body(self):
        context = self.context(kit_hook_files=('/kit/hooks.py',))
        command = "result=$(\napply_patch <<'EOF'\n*** Begin Patch\n*** Delete File: /kit/hooks.py\n*** End Patch\nEOF\n)\n"
        self.assertEqual(self.classify(command, context), g.Match('hook-disable'))
        self.assertIsNone(self.classify(command.replace('/kit/hooks.py', '/r/ordinary.py'), context))

    def test_f4_substitution_command_order(self):
        context = self.context(kit_hook_files=('/kit/hooks.py',))
        command = "result=$(\napply_patch <<'EOF'\n*** Begin Patch\n*** Delete File: /kit/hooks.py\n*** End Patch\nEOF\n)\n"
        self.assertEqual(self.classify('adt engine stop; ' + command, context), g.Match('operator-only'))
        self.assertEqual(self.classify(command + 'adt engine stop', context), g.Match('hook-disable'))

    def test_f4_substitution_occurrence_identity(self):
        context = self.context(kit_hook_files=('/kit/hooks.py',))
        command = "result=$(\napply_patch <<'EOF'\n*** Begin Patch\n*** Delete File: /kit/hooks.py\n*** End Patch\nEOF\n)\n"
        ordinary = "printf '%s' '__adt_'substitution_0__; "
        genuine = 'result=$(adt engine stop); '
        cases = ((ordinary + 'adt engine stop; ' + command, g.Match('operator-only')),
                 (ordinary + command + 'adt engine stop', g.Match('hook-disable')),
                 (ordinary, None),
                 (genuine + command, g.Match('operator-only')),
                 (command + genuine, g.Match('hook-disable')))
        for text, expected in cases:
            with self.subTest(command=text):
                self.assertEqual(self.classify(text, context), expected)


class TestAnswerRouteRules(unittest.TestCase):
    def test_bridge_call_shapes_refused(self):
        address = "http://localhost:8765/api/op/queue.answer"
        commands = (
            "curl -X POST http://127.0.0.1:8765/api/op/queue.answer -d '{}'",
            f"""python3 -c "import urllib.request; urllib.request.urlopen('{address}')""",
            "adt console open --print",
            "adt console open --ready PATH --pri",
            "adt console open --p",
            "adt console open --item ITEM --prin",
            "adt console open --pr --item ITEM",
        )
        calls = [("Bash", {"command": command}) for command in commands]
        calls.append(("navigate", {"url": "http://127.0.0.1:8765/#launch=" + "a" * 43}))
        for tool, payload in calls:
            with self.subTest(tool=tool, payload=payload):
                self.assertEqual(g.classify_tool(tool, payload, ()), g.Match("operator-endpoint"))

    def test_bridge_mentions_pass(self):
        address = "http://127.0.0.1:8765/api/op/queue.answer"
        text = f"/api/op/queue.answer {address}"
        bridge = "tools/relay_engine/api_operator_bridge.py"
        commands = (
            "git grep -n '/api/op/'",
            f"grep -n '/api/op/queue.answer' {bridge}",
            f"grep -n _PAGE_OPERATION_PREFIX {bridge}",
            "adt console open",
            "adt console open --ready=--print",
            "adt console open --ready /tmp/--pri",
            "curl http://notlocalhost:8765/api/op/queue.answer",
            "curl http://my-localhost:8765/api/op/queue.answer",
            "curl http://127.0.0.1:8765/api/read/queue.list",
            "curl http://127.0.0.1:3000/api/session",
            "curl http://localhost:5173/api/users",
        )
        calls = [("Bash", {"command": command}) for command in commands]
        calls.extend((
            ("Edit", {"new_string": text}),
            ("Write", {"content": text}),
            ("Edit", {"new_string": "Reference /home/operator/.adt/run/0123456789abcdef/open.html only"}),
            ("Grep", {"pattern": "/api/op/"}),
        ))
        for tool, payload in calls:
            with self.subTest(tool=tool, payload=payload):
                self.assertIsNone(g.classify_tool(
                    tool, payload, (), runtime_bases=("/home/operator/.adt/run",)))

    def test_launch_page_role_refused(self):
        home = "/home/operator"
        base = f"{home}/.adt/run"
        xdg = "/run/user/501/adt"
        runtime = "0123456789abcdef"
        positive = (
            ("Read", {"path": f"{base}/{runtime}/open.html"}),
            ("Read", {"path": f"{base}/{runtime.upper()}/OPEN.HTML"}),
            ("Read", {"path": f"{base.upper()}/{runtime}/open.html"}),
            ("NotebookRead", {"notebook_path": f"{base}/{runtime}/open.html"}),
            ("mcp__filesystem__read_file", {"uri": f"{base}/{runtime}/open.html"}),
            ("mcp__filesystem__read_text_file", {"source": f"{base}/{runtime}/open.html"}),
            ("Bash", {"command": f"cat {xdg}/{runtime}/open.html"}),
            ("Grep", {"path": base, "output_mode": "content"}),
            ("Grep", {"path": f"{base}/{runtime}", "output_mode": "content"}),
        )
        negative = (
            ("Grep", {"path": home, "output_mode": "content"}),
            ("Grep", {"path": f"{home}/.adt", "output_mode": "content"}),
            ("Glob", {"path": base}),
            ("list_directory", {"path": base}),
            ("Read", {"path": f"{base}/{runtime[:-1]}/open.html"}),
            ("Read", {"path": f"{base}/{runtime}/x/open.html"}),
            ("Read", {"path": f"{base}/{runtime}/ready.json"}),
            ("Read", {"path": f"{home}/open.html"}),
        )
        for tool, payload in positive:
            with self.subTest(tool=tool, payload=payload):
                match = g.classify_tool(tool, payload, (), runtime_bases=(base, xdg))
                self.assertIsNotNone(match)
                self.assertEqual(match.effect, "key-file-read")
        for tool, payload in negative:
            with self.subTest(tool=tool, payload=payload):
                self.assertIsNone(g.classify_tool(tool, payload, (), runtime_bases=(base, xdg)))
        for path, context in (
            (f"{runtime}/open.html", {"home": home}),
            (f"~/.adt/run/{runtime}/open.html", {"cwd": "/work"}),
        ):
            with self.subTest(unresolved_path=path):
                match = g.classify_tool("Read", {"path": path}, (), runtime_bases=(base, xdg),
                                        **context)
                self.assertIsNotNone(match)
                self.assertEqual(match.effect, "key-file-read")
                self.assertIn("unparsed", match.flags)
        for tool, payload in positive + negative:
            with self.subTest(tool=tool, payload=payload, bases=()):
                self.assertIsNone(g.classify_tool(tool, payload, (), runtime_bases=()))

    def test_bridge_literals_pinned(self):
        self.assertEqual(g.BRIDGE_OPERATION_PREFIX, api_operator_bridge._PAGE_OPERATION_PREFIX)
        self.assertIn(b"/" + g.LAUNCH_FRAGMENT.encode(), api_operator_bridge._PAGE_URL.pattern)
        self.assertEqual(g.LAUNCH_PAGE, api_operator_bridge._LAUNCH_PAGE)
        store_id = "0123456789abcdef0123456789abcdef"
        home = "/home/operator"
        with tempfile.TemporaryDirectory() as xdg:
            for environ, index in (({}, 0), ({"XDG_RUNTIME_DIR": xdg}, 1)):
                context = dict(platform="linux", environ=environ, home=home)
                with self.subTest(index=index):
                    base = lifecycle_paths.runtime_bases(**context)[index]
                    self.assertEqual(lifecycle_paths.runtime_dir(store_id, **context),
                                     f"{base}/{store_id[:16]}")
        tree = ast.parse(Path(g.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertFalse(any(alias.name.split(".")[0] == "relay_engine"
                                     for alias in node.names))
            elif isinstance(node, ast.ImportFrom):
                self.assertFalse(node.level or (node.module or "").split(".")[0] == "relay_engine")

    def test_runtime_bases_candidates(self):
        home = "/home/operator"
        fallback = (f"{home}/.adt/run",)
        xdg = "/run/user/501"
        for platform, raw, expected in (
            ("darwin", xdg, fallback),
            ("linux", xdg, (*fallback, f"{xdg}/adt")),
            ("linux", "relative", fallback),
            ("linux", "", fallback),
        ):
            with self.subTest(platform=platform, xdg=raw):
                self.assertEqual(lifecycle_paths.runtime_bases(
                    platform=platform, environ={"XDG_RUNTIME_DIR": raw}, home=home), expected)
        with self.assertRaisesRegex(ValueError, "^unsupported_platform$"):
            lifecycle_paths.runtime_bases(platform="win32", environ={}, home=home)

    def test_table_answer_route_shapes(self):
        table = g.table()
        self.assertEqual(table.get("refused_path_shapes"),
                         {"console launch page": "<base>/<16 lowercase hex>/open.html"})
        self.assertEqual(table["effects"]["operator-endpoint"]["command_shapes"], [
            "relay answer", "relay prompt-record", "adt console open --print",
            "bridge call address in command text or url input",
        ])
        self.assertEqual(table["effects"]["key-file-read"]["command_shapes"], [
            "tool string or list input naming a refused path",
            "content reader naming an ancestor directory",
            "command naming refused path",
            "console launch page shape under runtime bases",
        ])
        self.assertEqual(table["refused_paths"],
                         ["frank operator key", "Jev key", "prompt-hook key", "other seat keys"])
        self.assertEqual(table["design"], "DD-v3-authority-20261104-r22")
