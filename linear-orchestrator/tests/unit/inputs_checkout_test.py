import os
import re

from agent_pm import delegate
from agent_pm.linear_orchestrator.engine import inputs, target
from orchestrator_test_helpers.unit.inputs.checkout_fixtures import Checkout as CheckoutBase
from orchestrator_test_helpers.unit.inputs.data import DOCS, HUMANS, issue


class Checkout1(CheckoutBase):
    def test_a_build_prompts_repo_py_commands_take_the_inputs_checkout(self):
        t = target.Target("ophis", "agent-pm", "ENG-7-x")
        text = inputs.render(
            issue("D", ident="ENG-7"),
            "engineer",
            inputs.Sources((), None),
            humans=HUMANS,
            target=t,
            docs=DOCS,
        )
        name = re.search(r"^Checkout: (.+)$", text, re.M).group(1)
        self.assertEqual(
            os.path.basename(target.checkout("ENG-7", "ophis", "agent-pm", work="/w")),
            f"agent-pm-{name}",
        )
        for task in ("build", "light-build"):
            with self.subTest(task=task):
                run = delegate.resolve_run("engineer", task)
                spec = delegate.Spec(
                    text, self.home, task=task, out=os.path.join(self.home, "out.md")
                )
                prompt = delegate.plan(run, spec).prompt
                self.assertIn(f"\nCheckout: {name}\n", prompt)
                self.assertIn(
                    "`[--name <checkout>]` in a command → `--name <checkout>`, `<checkout>` the input's "
                    "`Checkout:`",
                    prompt,
                )
                for cmd in ("worktree", "status"):
                    self.assertIn(
                        f"python3 -P <team>/tools/repo.py {cmd} --dir <Workdir>/src --branch <branch> "
                        "[--name <checkout>] [--base <Base:>] <repo>`",
                        prompt,
                    )
