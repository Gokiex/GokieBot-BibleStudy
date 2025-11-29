import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from main import (
    app,
    BRISBANE_TZ,
    STUDY_HOUR,
    STUDY_MINUTE,
    get_next_study_time,
)


class DashboardViewTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_dashboard_renders(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)

        html = response.get_data(as_text=True)
        self.assertIn("Bible Study Schedule Dashboard", html)
        self.assertIn("jayden", html)
        self.assertIn("Sat 29/11", html)


class StudyTimeCalculationTests(unittest.TestCase):
    def test_next_study_skips_past_saturday_evening(self):
        """After the study time passes on Saturday, we schedule for the following week."""

        fake_now = BRISBANE_TZ.localize(datetime(2024, 8, 3, 21, 15))

        with patch("main.load_schedule", return_value=[]), patch("main.datetime") as mock_datetime:
            mock_datetime.now.return_value = fake_now

            next_study = get_next_study_time()

        self.assertEqual(next_study.weekday(), 5)  # Saturday
        self.assertEqual(next_study.hour, STUDY_HOUR)
        self.assertEqual(next_study.minute, STUDY_MINUTE)
        self.assertEqual(next_study.date(), (fake_now + timedelta(days=7)).date())


if __name__ == "__main__":
    unittest.main()
