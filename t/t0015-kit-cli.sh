#!/bin/sh

test_description='kit command-line name

The program can be invoked as "kit"; user-facing CLI text says "kit",
while the Git implementation, repository format and "git version"
output remain compatible.'

. ./test-lib.sh

test_expect_success 'kit --version names the program kit' '
	kit --version >actual &&
	test_grep "^kit version " actual &&
	kit version >actual &&
	test_grep "^kit version " actual
'

test_expect_success 'git --version output stays stable' '
	git --version >actual &&
	test_grep "^git version " actual &&
	test_grep ! "kit" actual
'

test_expect_success 'kit and git report the same version number' '
	kit --version | sed "s/^kit //" >kit.out &&
	git --version | sed "s/^git //" >git.out &&
	test_cmp git.out kit.out
'

test_expect_success 'top-level usage uses kit' '
	test_expect_code 1 kit >actual &&
	test_grep "^usage: kit \[-v | --version\]" actual &&
	test_grep "kit help -a" actual &&
	test_grep ! "git help" actual
'

test_expect_success 'unknown command error uses kit' '
	test_expect_code 1 kit -c help.autocorrect=never no-such-command 2>err &&
	test_grep "^kit: .no-such-command. is not a kit command. See .kit --help.\." err
'

test_expect_success 'subcommand usage uses kit' '
	kit commit -h >actual &&
	test_grep "^usage: kit commit " actual &&
	kit add -h >actual &&
	test_grep "^usage: kit add " actual
'

test_expect_success 'scripted command usage uses kit' '
	kit quiltimport -h >actual &&
	test_grep "^usage: kit quiltimport" actual
'

test_expect_success 'kit help lists commands as kit' '
	kit help >actual &&
	test_grep "^usage: kit " actual &&
	test_grep "common kit commands" actual
'

test_expect_success 'status hints use kit' '
	kit init hints &&
	>hints/untracked &&
	kit -C hints status >actual &&
	test_grep "use \"kit add <file>...\" to include" actual &&
	test_grep ! "use \"git add" actual
'

test_expect_success 'kit performs normal Git operations' '
	kit init repo &&
	test_path_is_dir repo/.git &&
	echo one >repo/file &&
	kit -C repo add file &&
	kit -C repo commit -m one &&
	echo two >>repo/file &&
	git -C repo commit -a -m two &&
	kit -C repo log --format=%s >actual &&
	test_write_lines two one >expect &&
	test_cmp expect actual &&
	kit -C repo fsck
'

test_expect_success 'repositories are interchangeable between kit and git' '
	git -C repo rev-parse HEAD >expect &&
	kit -C repo rev-parse HEAD >actual &&
	test_cmp expect actual &&
	test "$(git -C repo rev-parse --git-dir)" = ".git"
'

test_expect_success 'kit clone runs the git-upload-pack helper' '
	kit clone --no-local "file://$(pwd)/repo" clone &&
	git -C repo rev-parse HEAD >expect &&
	kit -C clone rev-parse HEAD >actual &&
	test_cmp expect actual
'

test_expect_success 'Git environment variables and config keys still apply' '
	GIT_DIR=repo/.git kit rev-parse HEAD >actual &&
	git -C repo rev-parse HEAD >expect &&
	test_cmp expect actual &&
	kit -C repo config set core.kitTestKey value &&
	echo value >expect &&
	git -C repo config get core.kitTestKey >actual &&
	test_cmp expect actual
'

test_expect_success 'repository-format messages are unchanged' '
	nongit test_expect_code 128 kit status 2>err &&
	test_grep "not a git repository" err
'

test_expect_success 'bundle format signature is unchanged' '
	kit -C repo bundle create ../repo.bundle HEAD &&
	head -n 1 repo.bundle >actual &&
	echo "# v2 git bundle" >expect &&
	test_cmp expect actual
'

test_expect_success 'documentation still describes git' '
	src="${GIT_SOURCE_DIR:-$TEST_DIRECTORY/..}" &&
	test_grep "git status" "$src/Documentation/git-status.adoc" &&
	test_grep ! "kit status" "$src/Documentation/git-status.adoc" &&
	test_grep "clone of git" "$src/README.md"
'

test_done
