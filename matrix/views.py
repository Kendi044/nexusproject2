from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout, authenticate, update_session_auth_hash
from django.contrib.auth.models import User
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib import messages
from django.db import transaction, IntegrityError
from django.db.models import F
from django.http import JsonResponse
from decimal import Decimal
from django.db.models import Sum
from .models import MemberProfile, Transaction, MatrixNode
from .forms import RegistrationForm
import string
import random
import uuid

from .models import MemberProfile, AdminRevenue, WithdrawalRequest 
from .logic import place_member_with_spillover, get_board_tree

def generate_unique_ref_id():
    chars = string.ascii_uppercase + string.digits
    return 'NFG-' + ''.join(random.choice(chars) for _ in range(6))

@login_required
def upgrade_board(request):
    """Allows a user to pay to move to the next board level without duplication"""
    profile = request.user.memberprofile
    
    board_configs = {
        2: {"fee": Decimal('150.00')},
        3: {"fee": Decimal('400.00')},
        4: {"fee": Decimal('1100.00')},
        5: {"fee": Decimal('3400.00')},
    }

    target_board = profile.current_board + 1
    
    if target_board in board_configs:
        config = board_configs[target_board]
        
        if profile.balance >= config['fee']:
            try:
                with transaction.atomic():
                    # 1. DOUBLE CHECK: Ensure the user isn't already on the target board
                    # (This prevents the 'roseflower' repetition you see in your screenshot)
                    if profile.current_board >= target_board:
                        messages.warning(request, "You are already active on this board.")
                        return redirect('dashboard')

                    # 2. Deduct fee and increment board level
                    # Using update() ensures we stay thread-safe
                    MemberProfile.objects.filter(pk=profile.pk).update(
                        balance=F('balance') - config['fee'],
                        current_board=target_board
                    )
                    
                    # 3. Update Admin Stats
                    revenue, _ = AdminRevenue.objects.get_or_create(id=1)
                    revenue.total_fees_collected += config['fee']
                    revenue.save()
                    
                    # 4. PLACE IN THE NEW BOARD
                    # Ensure place_member_with_spillover uses 'get_or_create' internally
                    place_member_with_spillover(profile, profile.sponser, target_board)
                    
                    # Refresh profile from DB to reflect changes
                    profile.refresh_from_db()
                    
                    messages.success(request, f"Successfully upgraded to Board {target_board}!")
                    
            except Exception as e:
                messages.error(request, f"Upgrade failed: {str(e)}")
        else:
            messages.error(request, "Insufficient balance to upgrade.")
    else:
        messages.error(request, "Maximum board level reached or invalid board level.")

    return redirect('dashboard')

@staff_member_required
def admin_activate_user(request, user_id):
    """Admin manually activates a user after verifying payment"""
    member = get_object_or_404(MemberProfile, id=user_id)
    if member.payment_status != 'paid':
        with transaction.atomic():
            member.payment_status = 'paid'
            member.is_active = True
            member.save() 
            # Place in Board 1
            place_member_with_spillover(member, member.sponser, 1)
            
        messages.success(request, f"User {member.user.username} activated and placed.")
    return redirect('admin_panel')

def register_view(request):
    ref_id_from_url = request.GET.get('ref_id', '') 
    
    if request.method == 'POST':
        form = RegistrationForm(request.POST)
        form_ref_id = request.POST.get('ref_id') # Get sponsor ID from hidden field

        if form.is_valid():
            # 1. Save the user (Form handles password matching & hashing)
            user = form.save()
            
            # 2. Setup the Profile
            profile, created = MemberProfile.objects.get_or_create(user=user)
            profile.full_name = request.POST.get('full_name')
            profile.ref_id = generate_unique_ref_id() 

            # 3. Sponsor Assignment
            sponsor = None
            if form_ref_id:
                sponsor = MemberProfile.objects.filter(ref_id=form_ref_id).first()
            
            # Default to Admin if no sponsor found
            if not sponsor:
                sponsor = MemberProfile.objects.filter(user__is_superuser=True).first()
            
            profile.sponser = sponsor
            profile.save() 
            
            # 4. Log in and redirect
            login(request, user)
            messages.success(request, f"Welcome {user.username}! Account created.")
            return redirect('dashboard')
        else:
            # If passwords don't match or username is taken, form.errors will be populated
            messages.error(request, "Please correct the errors below.")
    else:
        form = RegistrationForm()

    return render(request, 'matrix/register.html', {
        'form': form, 
        'ref_id': ref_id_from_url
    })

# Only allow the Admin/Superuser to see this page
@user_passes_test(lambda u: u.is_superuser)
def admin_payout_dashboard(request):
    # Get only the requests that haven't been paid yet
    pending_payouts = WithdrawalRequest.objects.filter(status='pending').order_by('created_at')
    
    if request.method == "POST":
        payout_id = request.POST.get('payout_id')
        action = request.POST.get('action') # 'mark_paid' or 'cancel'
        
        # Use get_object_or_404 for better error handling
        payout = get_object_or_404(WithdrawalRequest, id=payout_id)
        profile = payout.user.memberprofile

        if action == 'mark_paid':
            with transaction.atomic():
                # --- FIX 1: REMOVED THE BALANCE DEDUCTION ---
                # The balance was already deducted in request_withdrawal.
                # Do NOT subtract payout.amount here again.

                # --- FIX 2: REMOVED ADMIN REVENUE UPDATE ---
                # AdminRevenue was already updated during request_withdrawal.
                # Updating it here would double your profit records.

                # 1️⃣ Mark withdrawal paid
                payout.status = 'paid'
                payout.save()
                messages.success(request, f"Payout for {payout.user.username} marked as paid.")

        elif action == 'cancel':
            with transaction.atomic():
                # 2️⃣ Refund the user if the withdrawal is cancelled
                profile.wallet += payout.amount
                profile.save()

                payout.status = 'cancelled'
                payout.save()
                messages.warning(request, f"Payout for {payout.user.username} cancelled and refunded.")

        return redirect('admin_payout_dashboard')

    return render(request, 'matrix/admin_payouts.html', {'payouts': pending_payouts})
def submit_hash(request):
    if request.method == "POST":
        profile = request.user.memberprofile
        txid = request.POST.get('transaction_hash').strip()
        
        if not txid:
            messages.error(request, "Please provide a Transaction ID.")
            return redirect('pay_page')

        already_used = MemberProfile.objects.filter(transaction_hash=txid, payment_status='paid').exists()
        if already_used:
            messages.error(request, "Error: This Transaction ID has already been verified for another account.")
            return redirect('pay_page')
        
        other_pending = MemberProfile.objects.filter(
            transaction_hash=txid, 
            payment_status='pending'
        ).exclude(user=request.user).exists()

        if other_pending:
            messages.error(request, "This Transaction ID is currently pending verification by another user.")
            return redirect('dashboard')
       
        # 3. New Submission: SAVE to database
        try:
            profile = request.user.memberprofile
            profile.transaction_hash = txid
            profile.payment_status = 'pending'
            profile.save()
            
            messages.success(request, "Success! Your Transaction ID is submitted and pending admin approval.")
            return redirect('dashboard')
            
        except IntegrityError:
            # This triggers if 'unique=True' is violated by another 'pending' user
            messages.error(request, "This Transaction ID is already pending verification by another user.")
            return redirect('pay_page')

    return redirect('pay_page')
            

@login_required
def notify_admin_payment(request):
    if request.method == "POST":
        profile = request.user.memberprofile
        board_level = request.POST.get('board_level', '1')
        profile.payment_status = 'pending_verification'
        profile.save()
        # You could add a 'payment_submitted' field to your model later,
        # but for now, we'll just send a message.
        messages.success(request, "Admin has been notified! Your account will be activated once payment is verified.")
        
        return redirect('dashboard')
    
    return redirect('payment_page')

def logout_view(request):
    if request.method == 'POST':
        logout(request)
        return redirect('login')
    return redirect('dashboard') # Safety redirect if accessed via GET

@login_required
def profile_view(request):
    try:
        # Get the profile associated with the logged-in user
        profile = request.user.memberprofile
        
        # Generate the full referral link for the user to copy
        # This combines your website domain with the user's unique referral code
        domain = request.get_host()
        ref_link = f"{request.scheme}://{domain}/register/?ref_id={profile.ref_id}"
        password_form = PasswordChangeForm(request.user)

        if request.method == 'POST':
        # --- Handle Wallet Update ---
         if 'update_wallet' in request.POST:
            new_wallet = request.POST.get('wallet_address')
            profile.wallet_address = new_wallet #
            profile.save() #
            messages.success(request, "Wallet address updated!") #
            return redirect('profile') # Redirect to clear POST data

        # --- Handle Password Change ---
         elif 'update_password' in request.POST:
            password_form = PasswordChangeForm(request.user, request.POST) #
            if password_form.is_valid():
                user = password_form.save() #
                update_session_auth_hash(request, user) #
                messages.success(request, "Password updated successfully!") #
                return redirect('profile')
            else:
                messages.error(request, "Please correct the error below.") #
        context = {
            'profile': profile,
            'ref_link': ref_link,
            'password_form': password_form,
        }

        return render(request, 'matrix/profile.html', context)
    except MemberProfile.DoesNotExist:
        # Fallback if a user somehow exists without a profile
        messages.error(request, "Profile not found.")
        return redirect('dashboard')
    
@login_required
def request_withdrawal(request):
    profile = request.user.memberprofile
    # We will use 'history' to store the query
    history = WithdrawalRequest.objects.filter(user=request.user).order_by('-created_at')
    
    # Calculate pending count for the dashboard stat card
    pending_count = history.filter(status='Pending').count()

    if request.method == "POST":
        # PREVENT MULTIPLE ENTRIES: Block if they already have a Pending request
        if history.filter(status='Pending').exists():
            messages.error(request, "You already have a pending withdrawal.")
            return redirect('withdrawals')

        try:
            amount_val = request.POST.get('amount')
            address = request.POST.get('wallet_address')

            if amount_val and address:
                amount = Decimal(amount_val)
                if 10 <= amount <= profile.wallet: # Check against actual wallet balance
                    with transaction.atomic():
                        WithdrawalRequest.objects.create(
                            user=request.user,
                            amount=amount,
                            wallet_address=address,
                            status='Pending'
                        )
                    messages.success(request, "Request submitted! It will not be deducted until Admin pays.")
                    return redirect('withdrawals')
                else:
                    messages.error(request, "Invalid amount or insufficient balance.")
        except Exception as e:
            messages.error(request, "Error processing request. Please try again.")

    # IMPORTANT: The keys here must match what your HTML template uses
    context = {
        'profile': profile,
        'requests': history,      # Matches {% for req in requests %}
        'pending_count': pending_count # Matches {{ pending_count }}
    }
    return render(request, 'matrix/withdrawals.html', context)
def login_view(request):
    if request.method == 'POST':
        form = AuthenticationForm(data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            return redirect('dashboard')
    else:
        form = AuthenticationForm()
    return render(request, 'matrix/login.html', {'form': form})

@login_required
def dashboard_view(request):
    try:
        profile = request.user.memberprofile
    except MemberProfile.DoesNotExist:
        return render(request, 'matrix/error.html', {'message': 'Profile not found'})

    # 1. Get total board counts (Total people in their 2x2 matrix)
    counts = {
        'b1': profile.board_1_count_value or 0,
        'b2': profile.board_2_count or 0,
        'b3': profile.board_3_count or 0,
        'b4': profile.board_4_count or 0,
        'b5': profile.board_5_count or 0,
    }
    
    # 2. Calculate DIRECT Referrals per Board
    # This filters the user's referrals based on their current progress
    direct_referrals = {
        'd1': profile.referrals.filter(current_board=1).count(),
        'd2': profile.referrals.filter(current_board=2).count(),
        'd3': profile.referrals.filter(current_board=3).count(),
        'd4': profile.referrals.filter(current_board=4).count(),
        'd5': profile.referrals.filter(current_board=5).count(),
        'total_directs': profile.referrals.count()
    }
    
    # 3. Progress percentages (Calculating how close they are to cycling)
    # Since it's a 2x2 matrix, the goal is 6 people.
    percents = {
        f'{k}_percent': min((v / 6) * 100, 100) for k, v in counts.items()
    }
    
    # 4. Prepare Context for Template
    context = {
        'profile': profile,
        **counts,           # Expands b1, b2, etc.
        **direct_referrals, # Expands d1, d2, etc.
        **percents,         # Expands b1_percent, etc.
        
        # Tree Pointers for UI rendering (Shoulders)
        'nodes': {
            'b1': {'l': profile.left_child_b1, 'r': profile.right_child_b1},
            'b2': {'l': profile.left_child_b2, 'r': profile.right_child_b2},
            'b3': {'l': profile.left_child_b3, 'r': profile.right_child_b3},
            'b4': {'l': profile.left_child_b4, 'r': profile.right_child_b4},
            'b5': {'l': profile.left_child_b5, 'r': profile.right_child_b5},
        }
    }
    
    return render(request, 'matrix/dashboard.html', context)

@login_required
def payment_initiate(request):
    # Fetch the profile of the logged-in user
    profile = request.user.memberprofile
    
    # If the user is already active, send them to the dashboard
    if profile.is_active:
        return redirect('dashboard')

    context = {
        'profile': profile,
        'fee_amount': 55.00,  # Example: Entry fee for Board 1
    }
    return render(request, 'matrix/payment_page.html', context)

@login_required
def get_matrix_data(request):
    user_profile = request.user.memberprofile
    # Defaults to Board 1 if no level is specified
    board_level = int(request.GET.get('board', 1)) 
    
    # Define the configuration for all 5 boards
    # Key: board_level, Value: (Level Name, Payout, Count Field Name)
    board_configs = {
        1: ("Starter Board ($50)", "200.00", "board_1_count_value"),
        2: ("Basic Board ($150)", "600.00", "board_2_count"),
        3: ("Bronze Board ($400)", "1,600.00", "board_3_count"),
        4: ("Silver Board ($1,100)", "4,400.00", "board_4_count"),
        5: ("Gold Board ($3,400)", "13,600.00", "board_5_count"),
    }

    # Fetch configuration for the requested level
    if board_level in board_configs:
        name, payout, count_attr = board_configs[board_level]
        # Use getattr to dynamically fetch the count field from the profile
        current_count = getattr(user_profile, count_attr, 0)
        
        tree = {
            "level_name": name,
            "count": current_count,
            "target": 6,
            "payout": payout
        }
    else:
        return JsonResponse({"error": "Invalid board level"}, status=400)

    return JsonResponse(tree)

# --- ADMIN PANEL DATA ---

@login_required
def matrix_tree_view(request):
    p = request.user.memberprofile
    
    # Determine which board to display (default to Board 1)
    board = request.GET.get('board', '1')

    if board not in ['1', '2', '3', '4', '5']:
        board = '1'
        # --- Level 1 (Board 2) ---
    left_field = f'left_child_b{board}'
    right_field = f'right_child_b{board}'

    # --- Level 1 ---
    l1_left = getattr(p, left_field, None)
    l1_right = getattr(p, right_field, None)

    # --- Level 2 ---
    # We look at the children of our Level 1 nodes
    l2_ll = getattr(l1_left, left_field, None) if l1_left else None
    l2_lr = getattr(l1_left, right_field, None) if l1_left else None
    l2_rl = getattr(l1_right, left_field, None) if l1_right else None
    l2_rr = getattr(l1_right, right_field, None) if l1_right else None    
    
    context = {
        'head': p,
        'l1_left': l1_left,
        'l1_right': l1_right,
        'l2_ll': l2_ll,
        'l2_lr': l2_lr,
        'l2_rl': l2_rl,
        'l2_rr': l2_rr,
        'current_board': board
    }
    return render(request, 'matrix/matrix_tree.html', context)

@login_required
def admin_panel_page(request):
    if not request.user.is_staff:
        return redirect('dashboard') # Send regular users away
    return render(request, 'matrix/admin_panel.html')

@login_required
def admin_summary_view(request):
    if not request.user.is_staff:
        return JsonResponse({"error": "Unauthorized"}, status=403)

    stats = AdminRevenue.objects.first() 
    active_count = MemberProfile.objects.filter(is_active=True).count()
    
    if not stats:
        return JsonResponse({
            "platform_profit": "0.00",
            "active_members": active_count,
            "status": "No revenue data yet"
        })

    data = {
        "platform_profit": "{:.2f}".format(stats.total_fees_collected),
        "board_1_rev": "{:.2f}".format(stats.b1_fees),
        "board_2_rev": "{:.2f}".format(stats.b2_fees),
        "board_3_rev": "{:.2f}".format(stats.b3_fees), # Add these
        "board_4_rev": "{:.2f}".format(stats.b4_fees),
        "board_5_rev": "{:.2f}".format(stats.b5_fees),
        "active_members": active_count,
        "vault_health": "STABLE"
    }
    return JsonResponse(data) 

# views.py
def confirm_payment_view(request, profile_id):
    profile = get_object_or_404(MemberProfile, id=profile_id)
    
    # Logic to verify the payment actually happened...
    
    profile.payment_status = 'paid'
    profile.is_active = True
    profile.save() # Step 1: Save the status
    
    place_member_with_spillover(profile, profile.sponser, 1)

    return redirect('dashboard')

def generate_ref_id():
    return uuid.uuid4().hex[:10]

def airdrops_view(request):
    # Ensure you have a template named airdrops.html
    return render(request, 'matrix/airdrops.html', {'profile': request.user.memberprofile})

from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from django.contrib import messages

@login_required
def create_payment_invoice(request):
    profile = request.user.memberprofile

    # Prevent duplicate payment attempts
    if profile.payment_status == 'paid':
        messages.info(request, "Your account is already activated.")
        return redirect('dashboard')

    context = {
        'user': request.user,
        'profile': profile,
        'amount': profile.registration_fee,  # or fixed amount e.g. 1000
        'wallet_address': 'YOUR_ADMIN_WALLET_ADDRESS',
    }

    return render(request, 'matrix/pay_page.html', context)

# views.py
@login_required
def some_move_function(request):
    profile = request.user.memberprofile
    if profile.is_position_locked and not request.user.is_staff:
        messages.error(request, "Your position in the matrix is locked. Please contact Admin.")
        return redirect('dashboard')