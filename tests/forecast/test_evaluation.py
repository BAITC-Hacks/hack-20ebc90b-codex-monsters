"""Independent numerical expectations; no shared fixture or model internals."""

import unittest

from ekt.forecast.evaluation import (
    evaluate_forecast,
    evaluate_masked_recovery,
    evaluate_recovery,
    mase,
    rolling_origin_evaluation,
    signed_bias,
    wape,
)


class ForecastMetricsTests(unittest.TestCase):
    def test_known_arithmetic_and_signed_bias(self):
        # Absolute errors 2+4; signed errors 2-4; training naive error 2.
        report = evaluate_forecast([10, 20], [12, 16], train=[2, 4, 6, 8], uom="piece")
        self.assertAlmostEqual(report["wape"], 0.2)
        self.assertAlmostEqual(report["bias"], -2 / 30)
        self.assertAlmostEqual(report["mase"], 1.5)
        self.assertEqual(report["mase_train_scale"], 2)
        self.assertEqual(report["undefined"], {})
        self.assertGreater(signed_bias([10], [12]), 0)

    def test_mase_uses_only_training_denominator(self):
        self.assertEqual(mase([100, 200], [101, 199], [2, 4, 6]), 0.5)
        self.assertEqual(mase([1, 2], [2, 1], [2, 4, 6]), 0.5)
        self.assertEqual(mase([10], [14], [1, 10, 3, 12], seasonal_period=2), 2)

    def test_undefined_is_not_zero_error(self):
        report = evaluate_forecast([0, 0], [0, 0], train=[5, 5, 5], uom="piece")
        self.assertIsNone(report["wape"])
        self.assertIsNone(report["bias"])
        self.assertIsNone(report["mase"])
        self.assertEqual(report["undefined"]["wape"], "zero_actual_total")
        self.assertEqual(report["undefined"]["mase"], "zero_training_naive_error")
        self.assertIsNone(mase([1], [1], [1]))
        empty = evaluate_forecast([], [], train=[1, 2], uom="metre")
        self.assertEqual(empty["n"], 0)
        self.assertEqual(set(empty["undefined"].values()), {"empty_evaluation_sample"})

    def test_zero_holdout_can_have_defined_mase(self):
        report = evaluate_forecast([0], [2], train=[1, 3], uom="piece")
        self.assertIsNone(report["wape"])
        self.assertEqual(report["mase"], 1)

    def test_unit_guard_rejects_incompatible_aggregation(self):
        with self.assertRaisesRegex(ValueError, "incompatible UOMs"):
            evaluate_forecast([10, 20], [12, 16], train=[1, 2], uom=["piece", "metre"])
        report = evaluate_forecast([10, 20], [12, 16], train=[1, 2], uom=["metre", "metre"])
        self.assertEqual(report["uom"], "metre")

    def test_invalid_input_never_silently_becomes_zero(self):
        for actual in ([None], [float("nan")], [float("inf")]):
            with self.subTest(actual=actual), self.assertRaises(ValueError):
                wape(actual, [0])
        with self.assertRaisesRegex(ValueError, "equal lengths"):
            signed_bias([1, 2], [1])
        for period in (0, -1, 1.5, True):
            with self.subTest(period=period), self.assertRaises(ValueError):
                mase([1], [1], [1, 2], seasonal_period=period)


class TemporalEvaluationTests(unittest.TestCase):
    def test_two_origins_compare_recent_mean_with_actual_callback(self):
        received = []

        def linear_forecast(train, horizon):
            received.append(train)
            return [train[-1] + 2 * day for day in range(1, horizon + 1)]

        result = rolling_origin_evaluation(
            [2, 4, 6, 8, 10, 12, 14, 16], [4, 6], 2,
            linear_forecast, uom="piece", recent_window=2,
        )
        self.assertEqual(result["method"], "rolling_origin")
        self.assertEqual(received, [(2, 4, 6, 8), (2, 4, 6, 8, 10, 12)])
        first = result["origins"][0]
        self.assertEqual(first["models"]["recent_mean"]["predicted"], [7, 7])
        self.assertAlmostEqual(first["models"]["recent_mean"]["metrics"]["wape"], 8 / 22)
        for origin in result["origins"]:
            self.assertEqual(origin["models"]["final_baseline"]["metrics"]["wape"], 0)

    def test_future_injection_changes_scores_but_not_origin_prediction(self):
        forecast = lambda train, horizon: [train[-1]] * horizon
        original = rolling_origin_evaluation([1, 2, 3, 4, 5], [3], 2, forecast, uom="piece")
        changed = rolling_origin_evaluation([1, 2, 3, 400, 500], [3], 2, forecast, uom="piece")
        for name in ("recent_mean", "final_baseline"):
            before = original["origins"][0]["models"][name]
            after = changed["origins"][0]["models"][name]
            self.assertEqual(before["predicted"], after["predicted"])
            self.assertEqual(before["metrics"]["mase_train_scale"], after["metrics"]["mase_train_scale"])
            self.assertNotEqual(before["metrics"]["wape"], after["metrics"]["wape"])

    def test_rejects_missing_holdout_and_invalid_model_output(self):
        for origins in ([], [0], [2, 2], [4], [1.5]):
            with self.subTest(origins=origins), self.assertRaises(ValueError):
                rolling_origin_evaluation([1, 2, 3, 4], origins, 2, lambda train, horizon: [1, 1], uom="piece")
        for predictions in ([1], [1, float("nan")], [-1, 1]):
            with self.subTest(predictions=predictions), self.assertRaises(ValueError):
                rolling_origin_evaluation([1, 2, 3, 4], [2], 2, lambda train, horizon: predictions, uom="piece")


class RecoveryEvaluationTests(unittest.TestCase):
    def test_synthetic_latent_truth_is_independent(self):
        # Deliberately imperfect estimates; the hidden truth is not imputation.
        report = evaluate_recovery([8, 12], [10, 10], truth_source="synthetic_latent_truth", uom="piece")
        self.assertEqual(report["mae"], 2)
        self.assertEqual(report["wape"], 0.2)
        self.assertEqual(report["bias"], 0)

    def test_rejects_self_scoring_and_imputation_truth_source(self):
        recovered = [10, 10]
        with self.assertRaisesRegex(ValueError, "own imputation"):
            evaluate_recovery(recovered, recovered, truth_source="synthetic_latent_truth", uom="piece")
        with self.assertRaisesRegex(ValueError, "independent"):
            evaluate_recovery([10, 10], [10, 10], truth_source="model_imputation", uom="piece")

    def test_masked_truth_never_reaches_recovery_callback(self):
        received = []

        def donor_mean(masked):
            received.append(masked)
            donors = [value for value in masked if value is not None]
            mean = sum(donors) / len(donors)
            return [mean if value is None else value for value in masked]

        report = evaluate_masked_recovery([6, 10, 8, 14, 10], [1, 3], donor_mean, uom="piece")
        self.assertEqual(received, [(6, None, 8, None, 10)])
        self.assertEqual(report["n"], 2)
        self.assertEqual(report["mae"], 4)
        self.assertAlmostEqual(report["wape"], 8 / 24)
        self.assertEqual(report["truth_source"], "masked_in_stock")

    def test_recovery_zero_truth_remains_undefined_for_relative_metrics(self):
        report = evaluate_recovery([1], [0], truth_source="synthetic_latent_truth", uom="piece")
        self.assertEqual(report["mae"], 1)
        self.assertIsNone(report["wape"])
        self.assertIsNone(report["bias"])


if __name__ == "__main__":
    unittest.main()
