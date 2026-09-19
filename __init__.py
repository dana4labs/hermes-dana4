"""Dana4 — a serverless agent on the Dana4 collaborative platform."""

from pathlib import Path

from . import cli, tools


def register(ctx):
    tools.bind(ctx)

    for name, schema in tools.SCHEMAS.items():
        ctx.register_tool(
            name=name,
            toolset="dana4",
            schema=schema,
            handler=tools.HANDLERS[name],
            emoji=tools.EMOJI[name],
        )

    ctx.register_command(
        "dana4",
        handler=tools.slash_dana4,
        description="Dana4 connection, open tasks and blockers",
        args_hint="[status|help] [workspace_id]",
    )

    ctx.register_cli_command(
        name="dana4",
        help="Manage the Dana4 agent",
        setup_fn=cli.setup,
        handler_fn=cli.handle,
    )

    skills_dir = Path(__file__).parent / "skills"
    for child in sorted(skills_dir.iterdir()):
        skill_md = child / "SKILL.md"
        if child.is_dir() and skill_md.exists():
            ctx.register_skill(child.name, skill_md)
