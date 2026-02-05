import os
import django

# Set the settings module
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.contrib.auth.models import User

def create_superuser():
    username = 'admin'
    email = 'rosekendi044@gmail.com'
    password = 'Rosycandy22.' # Use your actual desired password

    if not User.objects.filter(username=username).exists():
        print(f"Creating superuser: {username}")
        User.objects.create_superuser(username, email, password)
        print("Superuser created successfully!")
    else:
        print("Superuser already exists.")

if __name__ == "__main__":
    create_superuser()