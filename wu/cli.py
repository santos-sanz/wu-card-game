from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from . import __version__, config
from .dataset import build_dataset, load_dataset, save_dataset
from .game import Rules, solve


def _parse_hand(value: str) -> list[int]:
    cards = [part.strip() for part in value.replace("[", "").replace("]", "").split(",") if part.strip()]
    try:
        return [int(card) for card in cards]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid hand: {value}") from exc


def _rules_from_args(args: argparse.Namespace) -> Rules:
    return Rules(
        must_use_all=not getattr(args, "no_must_use_all", False),
        allow_reuse=getattr(args, "allow_reuse", False),
        integer_only=not getattr(args, "no_integer_only", False),
    )


def _print_solution(hand: list[int], target: int, rules: Rules) -> int:
    stats: dict = {}
    solution = solve(hand, target, rules, stats=stats)
    print(f"hand={hand} target={target} rules={rules.to_dict()} nodes={stats.get('nodes')}")
    if solution is None:
        print("unsolvable")
        return 1
    print(f"solvable in {len(solution)} steps:")
    for index, step in enumerate(solution, start=1):
        print(f"  {index}. {step.to_dict()['text']}")
    values: list = []
    for step in solution:
        values.append(step.result)
    print(f"final={values[-1] if values else target}")
    return 0


def cmd_solve(args: argparse.Namespace) -> int:
    return _print_solution(_parse_hand(args.hand), args.target, _rules_from_args(args))


def cmd_make_dataset(args: argparse.Namespace) -> int:
    rules = _rules_from_args(args)
    records = build_dataset(
        count=args.count,
        seed=args.seed,
        target=args.target,
        rules=rules,
        solvable_only=not args.include_unsolvable,
    )
    save_dataset(records, args.out)
    solvable = sum(1 for record in records if record["solvable"])
    print(f"wrote {len(records)} hands to {args.out} ({solvable} solvable, {len(records) - solvable} unsolvable)")
    return 0


def cmd_benchmark(args: argparse.Namespace) -> int:
    from .benchmark import run_benchmark

    records = load_dataset(args.dataset)
    if args.limit:
        records = records[: args.limit]
    if not records:
        print(f"no hands in {args.dataset}; run `wu make-dataset` first", file=sys.stderr)
        return 2
    output = asyncio.run(
        run_benchmark(
            records,
            text_model=args.text_model,
            decisions_model=args.decisions_model,
            text_max_attempts=args.text_max_attempts,
            max_expansions=args.max_expansions,
            include_baseline=args.include_baseline,
            seed=args.seed,
            output_dir=args.output_dir,
        )
    )
    errors = [kind for kind, item in output["aggregate"].items() if item["errors"]]
    return 0 if not errors else 0


def cmd_stress(args: argparse.Namespace) -> int:
    from .benchmark import run_benchmark

    hand = sorted(args.hand)
    reference = solve(hand, args.target, Rules())
    if reference is None:
        print(f"hand {hand} target {args.target} is unsolvable; nothing to stress", file=sys.stderr)
        return 2
    records = [
        {
            "id": f"stress-{index + 1:03d}",
            "hand": hand,
            "target": args.target,
            "rules": Rules().to_dict(),
            "solvable": True,
            "steps": len(reference),
            "solution": [step.to_dict() for step in reference],
        }
        for index in range(args.runs)
    ]
    print(f"stress: {args.runs} repeated solvable hands {hand} target={args.target}")
    output = asyncio.run(
        run_benchmark(
            records,
            text_model=args.text_model,
            decisions_model=args.decisions_model,
            text_max_attempts=args.text_max_attempts,
            max_expansions=args.max_expansions,
            include_baseline=args.include_baseline,
            seed=args.seed,
            output_dir=args.output_dir,
        )
    )
    aggregate = output["aggregate"]
    text_ok = aggregate["text"]["solved"] == args.runs and aggregate["text"]["errors"] == 0
    decisions_ok = (
        aggregate["decisions"]["solved"] == args.runs and aggregate["decisions"]["errors"] == 0
    )
    print()
    print(f"text:      {aggregate['text']['solved']}/{args.runs} solved, "
          f"median {aggregate['text']['median_solved_ms']} ms")
    print(f"decisions: {aggregate['decisions']['solved']}/{args.runs} solved, "
          f"median {aggregate['decisions']['median_solved_ms']} ms")
    if text_ok and decisions_ok:
        print("STRESS PASS")
        return 0
    print("STRESS FAIL")
    return 1


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import run

    run(host=args.host, port=args.port, reload=args.reload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wu", description="Wu card game decision-vs-text model race")
    parser.add_argument("--version", action="version", version=f"wu {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="start the race control web app")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=cmd_serve)

    solve_cmd = sub.add_parser("solve", help="solve one hand with the exact solver")
    solve_cmd.add_argument("hand", type=str, help="comma-separated cards, e.g. 2,8,9,3,12,12")
    solve_cmd.add_argument("--target", type=int, default=config.DEFAULT_TARGET)
    solve_cmd.add_argument("--no-must-use-all", action="store_true")
    solve_cmd.add_argument("--no-integer-only", action="store_true")
    solve_cmd.add_argument("--allow-reuse", action="store_true")
    solve_cmd.set_defaults(func=cmd_solve)

    dataset = sub.add_parser("make-dataset", help="generate a reproducible hand dataset")
    dataset.add_argument("--count", type=int, default=50)
    dataset.add_argument("--seed", type=int, default=7)
    dataset.add_argument("--target", type=int, default=config.DEFAULT_TARGET)
    dataset.add_argument("--out", type=Path, default=config.DATA_DIR / "hands.jsonl")
    dataset.add_argument("--include-unsolvable", action="store_true")
    dataset.add_argument("--no-must-use-all", action="store_true")
    dataset.add_argument("--no-integer-only", action="store_true")
    dataset.add_argument("--allow-reuse", action="store_true")
    dataset.set_defaults(func=cmd_make_dataset)

    benchmark = sub.add_parser("benchmark", help="run both model families over the dataset")
    benchmark.add_argument("--dataset", type=Path, default=config.DATA_DIR / "hands.jsonl")
    benchmark.add_argument("--limit", type=int, default=None)
    benchmark.add_argument("--text-model", default=config.DEFAULT_TEXT_MODEL)
    benchmark.add_argument("--decisions-model", default=config.DEFAULT_DECISIONS_MODEL)
    benchmark.add_argument("--text-max-attempts", type=int, default=2)
    benchmark.add_argument("--max-expansions", type=int, default=120)
    benchmark.add_argument("--include-baseline", action="store_true")
    benchmark.add_argument("--seed", type=int, default=7)
    benchmark.add_argument("--output-dir", type=Path, default=config.RESULTS_DIR)
    benchmark.set_defaults(func=cmd_benchmark)

    stress = sub.add_parser("stress", help="repeatedly verify both lanes on a solvable hand")
    stress.add_argument("--runs", type=int, default=3)
    stress.add_argument("--hand", type=_parse_hand, default=[2, 8, 9, 3, 12, 12])
    stress.add_argument("--target", type=int, default=config.DEFAULT_TARGET)
    stress.add_argument("--text-model", default=config.DEFAULT_TEXT_MODEL)
    stress.add_argument("--decisions-model", default=config.DEFAULT_DECISIONS_MODEL)
    stress.add_argument("--text-max-attempts", type=int, default=2)
    stress.add_argument("--max-expansions", type=int, default=120)
    stress.add_argument("--include-baseline", action="store_true")
    stress.add_argument("--seed", type=int, default=11)
    stress.add_argument("--output-dir", type=Path, default=config.RESULTS_DIR)
    stress.set_defaults(func=cmd_stress)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
