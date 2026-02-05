from django.urls import path
from . import views
from django.shortcuts import render
def index_view(request):
    return render(request, 'matrix/index.html')

urlpatterns = [
    # Auth Pages
    path('register/', views.register_view, name='register'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('profile/', views.profile_view, name='profile'),
    
    # Dashboard & Matrix
    path('dashboard/', views.dashboard_view, name='dashboard'),
    path('api/matrix/', views.get_matrix_data, name='matrix_data'),
    
    # Payments
    path('activate/', views.create_payment_invoice, name='pay_page'),

    path('system-admin/', views.admin_panel_page, name='admin_panel'), # The HTML page
    path('admin-summary-data/', views.admin_summary_view, name='admin_api'), # The Data API
 
    path('matrix-tree/', views.matrix_tree_view, name='matrix_tree'),
    path('withdrawals/', views.request_withdrawal, name='withdrawals'),
    path('airdrops/', views.airdrops_view, name='airdrops'),

    path('management/payouts/', views.admin_payout_dashboard, name='admin_payout_dashboard'),
    path('profile/', views.profile_view, name='profile'),
    path('payment/', views.payment_initiate, name='pay_page'),
    path('payment/notify/', views.notify_admin_payment, name='notify_admin_payment'),
    path('payment/submit/', views.submit_hash, name='submit_hash'),
    path('', index_view, name='index'),
]