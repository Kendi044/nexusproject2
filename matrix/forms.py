from django import forms
from django.contrib.auth.models import User

class RegistrationForm(forms.ModelForm):
    # This field is NOT in the User model, so we define it manually
    ref_id = forms.CharField(
        required=False, 
        label="Referral Id (Optional for first user)",
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Enter Referral Id',
            # Ensure 'readonly' or 'disabled' is NOT here
        })
    )
    
    password = forms.CharField(widget=forms.PasswordInput(attrs={'class': 'form-control'}))
    confirm_password = forms.CharField(widget=forms.PasswordInput(attrs={'class': 'form-control'}))

    class Meta:
        model = User
        fields = ['username', 'email', 'password']

    def clean(self):
        cleaned_data = super().clean()
        p1 = cleaned_data.get("password")
        p2 = cleaned_data.get("confirm_password")

        if p1 != p2:
            raise forms.ValidationError("Passwords do not match!")
        return cleaned_data