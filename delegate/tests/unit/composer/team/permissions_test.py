"""Permissions behavior."""

import os
from unittest import mock

from agent_pm.delegate.composer.teams import (
    check_overlap,
    check_team_dirs,
    team_paths,
)

from delegate.tests.unit.composer.team.case import Fake
from delegate.tests.unit.composer.teams import POET, VERSE


class TeamDirChecks(Fake):
    """check_team_dirs, team_paths and check_overlap on a team dir under the test's HOME."""

    def setUp(self):
        super().setUp()
        self.top = self.team_dir(
            {"roles/poet.md": POET, "tasks/verse.md": VERSE, "templates/card.md": "# Card\n"}
        )

    def checked(self, writes=(), temp=()):
        return self.refused(check_team_dirs, [self.top], writes=list(writes), temp=temp)

    def test_team_paths_are_the_dir_its_kind_subdirs_and_their_md_files(self):
        self.write(self.top, {"tasks/notes.txt": "x", "methods/m.md": "# M\n"})
        self.assertEqual(
            team_paths(self.top),
            [
                self.top,
                self.path("roles"),
                self.path("roles/poet.md"),
                self.path("tasks"),
                self.path("tasks/verse.md"),
                self.path("templates"),
                self.path("templates/card.md"),
            ],
        )
        with mock.patch.object(os, "listdir", side_effect=PermissionError(13, "Permission denied")):
            self.assertEqual(
                self.refused(team_paths, self.top), f"{self.path('roles')}: Permission denied"
            )

    def test_no_team_dir_or_a_clean_one_passes(self):
        self.assertIsNone(check_team_dirs((), writes=[("the workdir", "/")], temp=None))
        self.assertIsNone(check_team_dirs([self.top], writes=[("the workdir", self.tmp)], temp=()))

    def test_a_team_dir_in_a_temp_dir(self):
        self.assertTrue(
            self.checked(temp=None).startswith(f"team_dirs: {self.top} is in temp dir ")
        )

    def test_a_team_dir_overlapping_a_dir_the_run_can_write(self):
        inside, home = os.path.join(self.top, "w"), os.path.realpath(self.agent_pm)
        for writes, want in (
            ([("the workdir", inside)], f"{self.top} overlaps the workdir {inside}"),
            ([("the workdir", self.agent_pm)], f"{self.top} overlaps the workdir {home}"),
            (
                [("the cwd", self.tmp), (f"write dir {inside}", inside)],
                f"{self.top} overlaps write dir {inside} {inside}",
            ),
            (
                [("a dir the runtime adds", home)],
                f"{self.top} overlaps a dir the runtime adds {home}",
            ),
        ):
            with self.subTest(want=want):
                self.assertEqual(
                    self.checked(writes), f"team_dirs: {want}: the agent run can write there"
                )

    def test_check_overlap_either_way_by_real_path(self):
        parent, sibling = os.path.dirname(self.top), self.top + "-other"
        self.assertIsNone(
            check_overlap("x", self.top, [("the cwd", sibling), ("the workdir", self.tmp)])
        )
        for p in (parent, os.path.join(self.top, "a", "b"), self.top):
            with self.subTest(p=p):
                self.assertEqual(
                    self.refused(check_overlap, "x", self.top, [("the cwd", p)]),
                    f"team_dirs: x overlaps the cwd {p}: the agent run can write there",
                )

    def test_a_team_dir_others_can_write(self):
        for rel, mode in (("tasks/verse.md", 0o664), ("tasks", 0o757), ("", 0o775)):
            with self.subTest(rel=rel):
                path = self.path(rel).rstrip("/")
                os.chmod(path, mode)
                self.assertEqual(
                    self.checked(),
                    f"team_dirs: {path} is writable by group or others: chmod go-w it",
                )
                os.chmod(path, 0o644 if rel.endswith(".md") else 0o755)
        check_team_dirs([self.top], writes=[], temp=())

    def test_a_team_dir_owned_by_another_user(self):
        me = os.getuid()
        with mock.patch.object(os, "getuid", return_value=me + 1):
            self.assertEqual(
                self.checked(),
                f"team_dirs: {self.top} is owned by uid {me}, not you (uid {me + 1}): chown it",
            )

    def test_a_parent_dir_others_could_replace_it_from(self):
        parent = os.path.dirname(self.top)
        os.chmod(parent, 0o775)
        self.assertEqual(
            self.checked(),
            f"team_dirs: {self.top}'s parent dir {parent} is writable by group or others: "
            "they could replace the team dir",
        )
        os.chmod(parent, 0o1777)
        check_team_dirs([self.top], writes=[], temp=())
        os.chmod(parent, 0o755)
        real = os.stat

        def stat_(path, *a, **kw):
            st = real(path, *a, **kw)
            return os.stat_result((*st[:4], 4242, *st[5:])) if os.fspath(path) == parent else st

        with mock.patch.object(os, "stat", stat_):
            self.assertEqual(
                self.checked(),
                f"team_dirs: {self.top}'s parent dir {parent} is owned by uid 4242, not "
                "you or root: they could replace the team dir",
            )
        check_team_dirs([self.top], writes=[], temp=())

    def test_a_symlink_is_checked_by_its_real_path(self):
        elsewhere = os.path.realpath(os.path.join(self.tmp, "elsewhere"))
        os.makedirs(elsewhere)
        link = self.path("tasks/verse.md")
        os.rename(link, os.path.join(elsewhere, "verse.md"))
        os.symlink(os.path.join(elsewhere, "verse.md"), link)
        check_team_dirs([self.top], writes=[], temp=())
        self.assertEqual(
            self.checked(temp=[elsewhere]),
            f"team_dirs: {link} ({elsewhere}/verse.md) is in temp dir "
            f"{elsewhere}: any process can write there",
        )
        work = os.path.realpath(os.path.join(self.tmp, "work"))
        tasks, moved = self.path("tasks"), os.path.join(work, "team-tasks")
        os.makedirs(work)
        os.rename(tasks, moved)
        os.symlink(moved, tasks)
        self.assertEqual(
            self.checked([("the workdir", work)]),
            f"team_dirs: {tasks} ({moved}) overlaps the workdir {work}: the agent run can write there",
        )
        os.chmod(os.path.join(elsewhere, "verse.md"), 0o666)
        self.assertEqual(
            self.checked(),
            f"team_dirs: {link} ({elsewhere}/verse.md) is writable by group or others: "
            "chmod go-w it",
        )
