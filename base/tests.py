from django.test import TestCase

class LoginTests(TestCase):
    def test_login(self):
        response = self.client.post(
            '/login/', {"email": "bekhruzrakhmonov2@gmail.com", "password": "feruzbek2007"})
        print("test response",response)
        self.assertEqual(response.status_code,200)
