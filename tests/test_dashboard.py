import unittest

from main import app


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


if __name__ == "__main__":
    unittest.main()
