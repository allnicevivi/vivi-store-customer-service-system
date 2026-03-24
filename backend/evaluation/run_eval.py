"""
evaluation/run_eval.py — 離線評估執行腳本

用途：每次系統優化後，在部署前執行此腳本確認品質未退步（regression test）。

執行方式（需先啟動 ChromaDB + 設定 GEMINI_API_KEY）：

    # Docker 環境（推薦）：
    docker-compose run --rm producer python /app/backend/evaluation/run_eval.py

    # 本地環境：
    PYTHONPATH=backend python backend/evaluation/run_eval.py

可選參數：
    --dataset PATH      指定 golden dataset 路徑（預設: evaluation/golden_dataset.json）
    --layer {1,2,3,all} 只跑指定評估層（預設: all）
    --mock              使用 keyword fallback（不呼叫 Gemini，快速驗證路由邏輯）
    --output PATH       輸出 JSON 結果路徑（預設: eval_results/run_<timestamp>.json）
    --sample N          只跑前 N 筆（開發期快速測試用）

評估層說明：
    Layer 1 - Routing：Router Agent 路由 & 意圖分類是否正確
    Layer 2 - Retrieval：FAQ/Policy 搜尋是否找到正確 chunk（需 ChromaDB）
    Layer 3 - Response：Resolution Agent 回覆品質（需 ChromaDB + Gemini）
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

# ── Eval isolation：防止 eval query 寫入正式 DB / Redis cache ─────────────
os.environ["EVAL_MODE"] = "1"

# ── PYTHONPATH 修正 ────────────────────────────────────────────────────────
_BACKEND_DIR = str(Path(__file__).parent.parent)
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from shared.schema import QueryMessage
from router_agent.router_agent import run_router_agent, _fallback_routing
from evaluation.llm_judge import LLMJudge

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("eval")

# Emotion band threshold
_EMOTION_THRESHOLDS = {
    "neutral":  (0.0, 0.3),
    "mild":     (0.3, 0.5),
    "moderate": (0.5, 0.7),
    "negative": (0.7, 1.0),
}


def _emotion_in_band(score: float, band: str) -> bool:
    """允許 ±1 個 band 的容忍（LLM scoring 有誤差）。"""
    bands = list(_EMOTION_THRESHOLDS.keys())
    expected_idx = bands.index(band) if band in bands else -1
    if expected_idx < 0:
        return True  # unknown band → skip check

    lo, hi = _EMOTION_THRESHOLDS[band]
    # 容忍相鄰 band（情緒評估本身有主觀性）
    if expected_idx > 0:
        lo = _EMOTION_THRESHOLDS[bands[expected_idx - 1]][0]
    if expected_idx < len(bands) - 1:
        hi = _EMOTION_THRESHOLDS[bands[expected_idx + 1]][1]
    return lo <= score <= hi


def _make_query_message(case: dict) -> QueryMessage:
    msg = QueryMessage(user_query=case["query"])
    msg.query_id = case["id"]
    return msg


# ── Layer 1: Routing Evaluation ──────────────────────────────────────────────
def run_layer1(case: dict, use_mock: bool) -> dict:
    """
    執行路由評估：
    - 路由 topic 是否正確
    - Intent 分類是否正確
    - Emotion band 是否在預期範圍內
    """
    msg = _make_query_message(case)

    try:
        if use_mock:
            actual_topic, reason = _fallback_routing(msg)
        else:
            actual_topic, reason = run_router_agent(msg)
    except Exception as e:
        return {
            "passed": False,
            "error": str(e),
            "actual_route": None,
            "actual_intent": None,
            "actual_emotion_score": None,
        }

    expected_route = case["expected_route"]
    expected_intent = case["expected_intent"]
    expected_emotion_band = case.get("expected_emotion_band", "")

    route_ok = actual_topic == expected_route
    intent_ok = msg.intent == expected_intent
    emotion_ok = _emotion_in_band(msg.emotion_score, expected_emotion_band)

    return {
        "passed": route_ok and intent_ok,
        "route_ok": route_ok,
        "intent_ok": intent_ok,
        "emotion_ok": emotion_ok,
        "actual_route": actual_topic,
        "actual_intent": msg.intent,
        "actual_emotion_score": round(msg.emotion_score, 3),
        "expected_route": expected_route,
        "expected_intent": expected_intent,
        "routing_reason": reason,
        "error": "",
    }


# ── Layer 2: Retrieval Evaluation ─────────────────────────────────────────────
def run_layer2(case: dict) -> dict:
    """
    評估 ChromaDB 搜尋品質：
    - 僅在 kb_answerable=true 的案例執行
    - 檢查 expected_answer_contains 中的關鍵字是否出現在前 3 筆搜尋結果中
    """
    if not case.get("kb_answerable", False):
        return {"skipped": True, "reason": "kb_answerable=false"}

    expected_keywords = case.get("expected_answer_contains", [])
    if not expected_keywords:
        return {"skipped": True, "reason": "no expected_answer_contains"}

    try:
        from shared.chroma_init import search_faq, search_policy
        query = case["query"]

        faq_hits = search_faq(query, n_results=3, threshold=0.0)
        policy_hits = search_policy(query, n_results=3, threshold=0.0)

        combined_docs = " ".join(
            h.get("document", "") for h in faq_hits + policy_hits
        )

        keyword_hits = [kw for kw in expected_keywords if kw in combined_docs]
        recall = len(keyword_hits) / len(expected_keywords) if expected_keywords else 1.0

        best_faq_score = faq_hits[0]["score"] if faq_hits else 0.0
        best_policy_score = policy_hits[0]["score"] if policy_hits else 0.0

        return {
            "passed": recall >= 0.5,
            "recall_at_3": round(recall, 3),
            "keywords_found": keyword_hits,
            "keywords_missing": [kw for kw in expected_keywords if kw not in combined_docs],
            "best_faq_score": round(best_faq_score, 4),
            "best_policy_score": round(best_policy_score, 4),
            "faq_result_count": len(faq_hits),
            "error": "",
        }
    except Exception as e:
        return {"passed": False, "error": str(e)}


# ── Layer 3: Response Quality Evaluation ─────────────────────────────────────
def run_layer3(case: dict, judge: LLMJudge) -> dict:
    """
    使用 LLM-as-Judge 評估 Resolution Agent 回覆品質。
    僅對 expected_route=queries.agent 的案例執行。
    """
    if case["expected_route"] != "queries.agent":
        return {"skipped": True, "reason": "not a queries.agent case"}

    if not judge.available:
        return {"skipped": True, "reason": "GEMINI_API_KEY not set"}

    try:
        from shared.chroma_init import search_faq, search_policy
        from resolution_agent.resolution_agent import run_resolution_agent

        msg = _make_query_message(case)
        # 先跑 routing 取得 intent 等 metadata
        try:
            run_router_agent(msg)
        except Exception:
            _fallback_routing(msg)

        # 呼叫 Resolution Agent
        target_topic, answer = run_resolution_agent(msg)

        if target_topic == "queries.human" or not answer:
            return {
                "passed": True,  # 正確升級人工不算失敗
                "escalated": True,
                "actual_answer": None,
                "error": "",
                "judge_scores": None,
            }

        # 取得 retrieved context 供 judge 評分
        faq_hits = search_faq(case["query"], n_results=3, threshold=0.0)
        policy_hits = search_policy(case["query"], n_results=2, threshold=0.0)
        retrieved_context = "\n\n".join(
            h.get("document", "") for h in faq_hits + policy_hits
        )

        judge_result = judge.evaluate(
            query=case["query"],
            response=answer,
            retrieved_context=retrieved_context,
            query_id=case["id"],
        )

        # 額外檢查 must_not_contain
        must_not = case.get("must_not_contain", [])
        forbidden_found = [w for w in must_not if w in (answer or "")]

        return {
            "passed": judge_result["passed"] and not forbidden_found,
            "escalated": False,
            "actual_answer": answer[:300] if answer else None,
            "judge_scores": judge_result,
            "forbidden_found": forbidden_found,
            "error": judge_result.get("error", ""),
        }
    except Exception as e:
        logger.error(f"Layer 3 error for {case['id']}: {e}", exc_info=True)
        return {"passed": False, "error": str(e)}


# ── 主流程 ──────────────────────────────────────────────────────────────────
def run_eval(
    dataset_path: str,
    layers: list[int],
    use_mock: bool,
    sample: int | None,
) -> dict:
    with open(dataset_path, encoding="utf-8") as f:
        dataset = json.load(f)

    cases = dataset["cases"]
    if sample:
        cases = cases[:sample]

    judge = LLMJudge()
    results = []

    layer1_results = []
    layer2_results = []
    layer3_results = []

    print(f"\n{'='*60}")
    print(f"  ViviStore E-Commerce Customer Service — Evaluation Run")
    print(f"  Dataset: {Path(dataset_path).name} ({len(cases)} cases)")
    print(f"  Layers:  {layers}  |  Mock: {use_mock}")
    print(f"{'='*60}\n")

    for i, case in enumerate(cases, 1):
        print(f"[{i:3d}/{len(cases)}] {case['id']:<25} ", end="", flush=True)
        case_result = {"id": case["id"], "query": case["query"][:50], "category": case["category"]}

        if 1 in layers:
            l1 = run_layer1(case, use_mock)
            case_result["layer1"] = l1
            layer1_results.append(l1)
            status = "✓" if l1.get("passed") else "✗"
            print(f"L1:{status} ", end="", flush=True)

        if 2 in layers:
            l2 = run_layer2(case)
            case_result["layer2"] = l2
            if not l2.get("skipped"):
                layer2_results.append(l2)
            status = "✓" if l2.get("passed") else ("⊘" if l2.get("skipped") else "✗")
            print(f"L2:{status} ", end="", flush=True)

        if 3 in layers:
            l3 = run_layer3(case, judge)
            case_result["layer3"] = l3
            if not l3.get("skipped"):
                layer3_results.append(l3)
            status = "✓" if l3.get("passed") else ("⊘" if l3.get("skipped") else "✗")
            print(f"L3:{status}", end="", flush=True)

        print()
        results.append(case_result)

    # ── 計算摘要指標 ──
    summary = {
        "run_at": datetime.now().isoformat(),
        "dataset": dataset_path,
        "total_cases": len(cases),
        "layers_run": layers,
        "use_mock": use_mock,
    }

    if layer1_results:
        route_ok = [r for r in layer1_results if r.get("route_ok")]
        intent_ok = [r for r in layer1_results if r.get("intent_ok")]
        emotion_ok = [r for r in layer1_results if r.get("emotion_ok")]
        summary["layer1"] = {
            "routing_accuracy": round(len(route_ok) / len(layer1_results), 3),
            "intent_accuracy":  round(len(intent_ok) / len(layer1_results), 3),
            "emotion_accuracy": round(len(emotion_ok) / len(layer1_results), 3),
            "total": len(layer1_results),
            "route_pass": len(route_ok),
            "intent_pass": len(intent_ok),
        }

    if layer2_results:
        recalls = [r["recall_at_3"] for r in layer2_results if "recall_at_3" in r]
        pass_count = sum(1 for r in layer2_results if r.get("passed"))
        summary["layer2"] = {
            "faq_recall_at_3": round(sum(recalls) / len(recalls), 3) if recalls else 0,
            "pass_rate": round(pass_count / len(layer2_results), 3),
            "total_evaluated": len(layer2_results),
        }

    if layer3_results:
        scored = [r for r in layer3_results if r.get("judge_scores") and not r["judge_scores"].get("error")]
        pass_count = sum(1 for r in layer3_results if r.get("passed"))
        if scored:
            dims = ["correctness", "completeness", "groundedness", "actionability", "overall"]
            avg_scores = {
                d: round(sum(r["judge_scores"][d] for r in scored) / len(scored), 2)
                for d in dims
            }
        else:
            avg_scores = {}
        summary["layer3"] = {
            "pass_rate": round(pass_count / len(layer3_results), 3),
            "total_evaluated": len(layer3_results),
            "avg_judge_scores": avg_scores,
            "escalation_count": sum(1 for r in layer3_results if r.get("escalated")),
        }

    # ── 失敗案例彙整（方便 drill-down）──
    failures = []
    for case, result in zip(cases, results):
        failed_layers = {}

        l1 = result.get("layer1")
        if l1 and not l1.get("passed") and not l1.get("skipped"):
            reason = "wrong_route" if not l1.get("route_ok") else "wrong_intent"
            failed_layers["layer1"] = {
                "reason": reason,
                "actual_route": l1.get("actual_route"),
                "expected_route": l1.get("expected_route"),
                "actual_intent": l1.get("actual_intent"),
                "expected_intent": l1.get("expected_intent"),
                "error": l1.get("error", ""),
            }

        l2 = result.get("layer2")
        if l2 and not l2.get("passed") and not l2.get("skipped"):
            failed_layers["layer2"] = {
                "reason": "low_recall",
                "recall_at_3": l2.get("recall_at_3"),
                "keywords_missing": l2.get("keywords_missing", []),
                "error": l2.get("error", ""),
            }

        l3 = result.get("layer3")
        if l3 and not l3.get("passed") and not l3.get("skipped"):
            js = l3.get("judge_scores") or {}
            failed_layers["layer3"] = {
                "reason": "judge_score_low",
                "overall": js.get("overall"),
                "critique": js.get("critique", ""),
                "forbidden_found": l3.get("forbidden_found", []),
                "error": l3.get("error", ""),
            }

        if failed_layers:
            failures.append({
                "id": case["id"],
                "query": case["query"],
                "category": case.get("category", ""),
                "tags": case.get("tags", []),
                "failed_layers": failed_layers,
            })

    # ── Category breakdown ──
    from collections import defaultdict
    cat_stats: dict = defaultdict(lambda: {"total": 0, "layer1_pass": 0, "layer2_pass": 0, "layer3_pass": 0})
    for case, result in zip(cases, results):
        cat = case.get("category", "unknown")
        cat_stats[cat]["total"] += 1
        l1 = result.get("layer1")
        if l1 and not l1.get("skipped") and l1.get("passed"):
            cat_stats[cat]["layer1_pass"] += 1
        l2 = result.get("layer2")
        if l2 and not l2.get("skipped") and l2.get("passed"):
            cat_stats[cat]["layer2_pass"] += 1
        l3 = result.get("layer3")
        if l3 and not l3.get("skipped") and l3.get("passed"):
            cat_stats[cat]["layer3_pass"] += 1
    category_breakdown = dict(cat_stats)

    return {"summary": summary, "results": results, "failures": failures, "category_breakdown": category_breakdown}


def _print_summary(summary: dict):
    print(f"\n{'='*60}")
    print("  EVALUATION SUMMARY")
    print(f"{'='*60}")

    if "layer1" in summary:
        l1 = summary["layer1"]
        print(f"\n[Layer 1 — Routing]  ({l1['total']} cases)")
        print(f"  Routing accuracy : {l1['routing_accuracy']:.1%}  ({l1['route_pass']}/{l1['total']})")
        print(f"  Intent accuracy  : {l1['intent_accuracy']:.1%}  ({l1['intent_pass']}/{l1['total']})")
        print(f"  Emotion accuracy : {l1['emotion_accuracy']:.1%}")

    if "layer2" in summary:
        l2 = summary["layer2"]
        print(f"\n[Layer 2 — Retrieval]  ({l2['total_evaluated']} KB-answerable cases)")
        print(f"  FAQ Recall@3     : {l2['faq_recall_at_3']:.3f}")
        print(f"  Pass rate        : {l2['pass_rate']:.1%}")

    if "layer3" in summary:
        l3 = summary["layer3"]
        print(f"\n[Layer 3 — Response Quality]  ({l3['total_evaluated']} cases)")
        print(f"  Pass rate        : {l3['pass_rate']:.1%}")
        print(f"  Escalated        : {l3['escalation_count']}")
        scores = l3.get("avg_judge_scores", {})
        if scores:
            print(f"  Avg scores (1–5):")
            for dim, val in scores.items():
                print(f"    {dim:<15}: {val:.2f}")

    print(f"\n{'='*60}\n")


def _write_markdown_report(path: str, report: dict) -> None:
    summary = report["summary"]
    failures = report.get("failures", [])
    category_breakdown = report.get("category_breakdown", {})
    lines = []

    lines.append("# Evaluation Report")
    lines.append(f"\n**Run at:** {summary.get('run_at', '')}  ")
    lines.append(f"**Dataset:** {Path(summary['dataset']).name}  ")
    lines.append(f"**Total cases:** {summary['total_cases']}  ")
    lines.append(f"**Elapsed:** {summary.get('elapsed_seconds', '?')}s  ")
    lines.append(f"**Mock mode:** {summary.get('use_mock', False)}  \n")

    # Summary metrics
    lines.append("## Summary Metrics\n")
    if "layer1" in summary:
        l1 = summary["layer1"]
        lines.append(f"### Layer 1 — Routing ({l1['total']} cases)")
        lines.append(f"| Metric | Value |")
        lines.append(f"|--------|-------|")
        lines.append(f"| Routing accuracy | {l1['routing_accuracy']:.1%} ({l1['route_pass']}/{l1['total']}) |")
        lines.append(f"| Intent accuracy  | {l1['intent_accuracy']:.1%} ({l1['intent_pass']}/{l1['total']}) |")
        lines.append(f"| Emotion accuracy | {l1['emotion_accuracy']:.1%} |\n")
    if "layer2" in summary:
        l2 = summary["layer2"]
        lines.append(f"### Layer 2 — Retrieval ({l2['total_evaluated']} cases)")
        lines.append(f"| Metric | Value |")
        lines.append(f"|--------|-------|")
        lines.append(f"| FAQ Recall@3 | {l2['faq_recall_at_3']:.3f} |")
        lines.append(f"| Pass rate    | {l2['pass_rate']:.1%} |\n")
    if "layer3" in summary:
        l3 = summary["layer3"]
        lines.append(f"### Layer 3 — Response Quality ({l3['total_evaluated']} cases)")
        lines.append(f"| Metric | Value |")
        lines.append(f"|--------|-------|")
        lines.append(f"| Pass rate   | {l3['pass_rate']:.1%} |")
        lines.append(f"| Escalations | {l3['escalation_count']} |")
        for dim, val in l3.get("avg_judge_scores", {}).items():
            lines.append(f"| Avg {dim} | {val:.2f} |")
        lines.append("")

    # Category breakdown
    if category_breakdown:
        lines.append("## Category Breakdown\n")
        lines.append("| Category | Total | L1 Pass | L2 Pass | L3 Pass |")
        lines.append("|----------|-------|---------|---------|---------|")
        for cat, stats in sorted(category_breakdown.items()):
            t = stats["total"]
            lines.append(
                f"| {cat} | {t} "
                f"| {stats['layer1_pass']}/{t} "
                f"| {stats['layer2_pass']}/{t} "
                f"| {stats['layer3_pass']}/{t} |"
            )
        lines.append("")

    # Failures
    lines.append(f"## Failures ({len(failures)} cases)\n")
    if not failures:
        lines.append("_All cases passed._\n")
    else:
        lines.append("| ID | Category | Query | Failed Layers | Details |")
        lines.append("|----|----------|-------|---------------|---------|")
        for f in failures:
            layer_names = ", ".join(f["failed_layers"].keys())
            details_parts = []
            for layer, info in f["failed_layers"].items():
                if layer == "layer1":
                    details_parts.append(
                        f"L1: {info.get('reason')} "
                        f"({info.get('actual_route','?')} vs {info.get('expected_route','?')})"
                    )
                elif layer == "layer2":
                    missing = info.get("keywords_missing", [])
                    details_parts.append(f"L2: recall={info.get('recall_at_3','?')} missing={missing}")
                elif layer == "layer3":
                    details_parts.append(
                        f"L3: overall={info.get('overall','?')} — {info.get('critique','')}"
                    )
            query_short = f["query"][:40].replace("|", "\\|")
            details_str = "; ".join(details_parts).replace("|", "\\|")
            lines.append(f"| {f['id']} | {f['category']} | {query_short} | {layer_names} | {details_str} |")
        lines.append("")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description="ViviStore Evaluation Runner")
    parser.add_argument(
        "--dataset",
        default=os.path.join(os.path.dirname(__file__), "golden_dataset.json"),
        help="Path to golden dataset JSON",
    )
    parser.add_argument(
        "--layer",
        choices=["1", "2", "3", "all"],
        default="all",
        help="Evaluation layer(s) to run (default: all)",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use keyword fallback instead of Gemini for routing (Layer 1)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output JSON path (default: eval_results/run_<timestamp>.json)",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=None,
        help="Only run first N cases (for quick dev testing)",
    )
    args = parser.parse_args()

    layers = [1, 2, 3] if args.layer == "all" else [int(args.layer)]

    output_path = args.output
    if not output_path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = os.path.join(os.path.dirname(__file__), "../../eval_results")
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, f"run_{ts}.json")

    start = time.time()
    report = run_eval(
        dataset_path=args.dataset,
        layers=layers,
        use_mock=args.mock,
        sample=args.sample,
    )
    elapsed = round(time.time() - start, 1)
    report["summary"]["elapsed_seconds"] = elapsed

    _print_summary(report["summary"])

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"Results saved to: {output_path}")

    # ── Markdown 報告 ──
    md_path = output_path.replace(".json", "_report.md")
    _write_markdown_report(md_path, report)
    print(f"Report  saved to: {md_path}")


if __name__ == "__main__":
    main()
