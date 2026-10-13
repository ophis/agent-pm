import os
from dataclasses import replace

from agent_pm.linear_orchestrator.linear import writeback
from orchestrator_test_helpers.unit.writeback.base_fixtures import Base
from orchestrator_test_helpers.unit.writeback.data import outcome
from orchestrator_test_helpers.unit.writeback.gql_fixtures import Gql


class FileRecheck(Base):
    def finish_with(self, ctx, path):
        gql = Gql()
        writeback.finish(
            replace(ctx, gql=gql), outcome("failed", "ENG-7: x", "push not permitted", files=[path])
        )
        posted = [c for c in gql.calls if c[0] == "comment"]
        return gql, posted[-1][1]["b"]

    def symlink(self, ctx):
        spec, _ = self.files(ctx)
        os.symlink(spec, os.path.join(ctx.workdir, "link.md"))
        return os.path.join(ctx.workdir, "link.md")

    def hard_link(self, ctx):
        spec, _ = self.files(ctx)
        os.link(spec, os.path.join(ctx.workdir, "copy.md"))
        return spec

    def outside(self, _ctx):
        outside = os.path.join(self.tmp, "outside.md")
        with open(outside, "w") as f:
            f.write("secret")
        return outside

    def linked_dir(self, ctx):
        os.makedirs(os.path.join(self.tmp, "away"))
        with open(os.path.join(self.tmp, "away", "x.md"), "w") as f:
            f.write("secret")
        os.symlink(os.path.join(self.tmp, "away"), os.path.join(ctx.workdir, "away"))
        return os.path.join(ctx.workdir, "away", "x.md")

    def fifo(self, ctx):
        os.mkfifo(os.path.join(ctx.workdir, "pipe.md"))
        return os.path.join(ctx.workdir, "pipe.md")
