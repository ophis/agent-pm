"""new/core/src/utils/repos.py: repo specs, origin URLs and the checkout path."""

from new.tests import SRC  # noqa: F401

import os
import unittest

from utils import repos


class Parse(unittest.TestCase):
    def test_forms(self):
        for spec, want in (
            ("o/n", ("github.com", "o", "n")),
            ("ghe.example.com/o/n", ("ghe.example.com", "o", "n")),
            ("https://github.com/o/n.git", ("github.com", "o", "n")),
            ("https://GHE.io/o/n/", ("ghe.io", "o", "n")),
        ):
            self.assertEqual(repos.Repo.parse(spec), repos.Repo(*want), spec)

    def test_unreadable(self):
        for spec in ("n", "o/..", "https://o/n", "o/n; rm -rf /", "-o/n"):
            with self.assertRaises(repos.Invalid, msg=spec):
                repos.Repo.parse(spec)

    def test_url_forms(self):
        """url_slug keeps the case as written; origin lowercases it."""
        for url, want in (
            ("git@GitHub.com:O/N.git", "GitHub.com/O/N"),
            ("https://ghe.io/o/n", "ghe.io/o/n"),
            ("ssh://git@ghe.io:2222/o/n.git", "ghe.io/o/n"),
            ("ssh://ghe.io/o/n", "ghe.io/o/n"),
            ("https://user@github.com/o/n", "github.com/o/n"),
            ("https://user:tok@github.com/o/n.git", "github.com/o/n"),
            ("http://ghe.io/o/n/", "ghe.io/o/n"),
            ("https://ghe.io:8443/o/n", "ghe.io:8443/o/n"),
        ):
            with self.subTest(url=url):
                self.assertEqual(repos.url_slug(url), want)
                self.assertEqual(repos.origin(url), want.lower())
                repos.Repo.parse(want)
        for url in (
            "/tmp/origin.git",
            "ftp://ghe.io/o/n",
            "https://ghe.io/o",
            "ssh://git@ghe.io:x/o/n",
            "https://ghe.io/o/n/extra",
        ):
            with self.subTest(url=url):
                self.assertEqual((repos.url_slug(url), repos.origin(url)), (None, None))


class Checkout(unittest.TestCase):
    def test_checkout_is_the_path_rule(self):
        self.assertEqual(repos.checkout("/b", "o", "n", "TASK-1"), "/b/o/n-TASK-1")
        self.assertEqual(
            repos.checkout("b", "o", "n", "x.y_z-1"),
            os.path.join(os.getcwd(), "b", "o", "n-x.y_z-1"),
        )
        with self.assertRaises(repos.Invalid):
            repos.checkout("/b", "o", "n", "a/b")


if __name__ == "__main__":
    unittest.main()
