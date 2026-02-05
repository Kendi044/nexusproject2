from django.contrib import admin, messages
from django.shortcuts import redirect, render
from .models import MemberProfile, AdminRevenue, WithdrawalRequest, MatrixNode
from django.utils.html import format_html 
from django.db import transaction  # Needed for atomic balance deduction
from .logic import  get_board_tree, sync_board_count, place_member_with_spillover
from decimal import Decimal
from django.db.models import F, Q
from django.urls import path

@admin.action(description='Verify Payment and Place in Matrix')
def activate_members(modeladmin, request, queryset):
    count = 0
    for profile in queryset:
        if profile.payment_status != 'paid':
          with transaction.atomic():
            profile.payment_status = 'paid'
            profile.is_active = True
            profile.is_already_placed_in_b1 = True # Prevent signal double-firing
            profile.save()
            
            # 2. Place and Trigger Counts
            # This must find the sponsor and then call the counting logic
            if profile.sponser:
                place_member_with_spillover(profile, profile.sponser, 1)
            count += 1
    modeladmin.message_user(request, f"Activated {count} members and updated board counts.")

@admin.action(description='Approve Withdrawal: Deduct Balance & Mark PAID')
def approve_withdrawal_action(modeladmin, request, queryset):
    for withdrawal in queryset:
        if withdrawal.status.lower() == 'pending':
            try:
                with transaction.atomic():
                    profile = withdrawal.user.memberprofile
                    if profile.balance >= withdrawal.amount:
                        # 1. Deduct from User
                        MemberProfile.objects.filter(pk=profile.pk).update(
                            balance=F('balance') - withdrawal.amount,
                            wallet=F('wallet') - withdrawal.amount
                        )
                        
                        # 2. Update Platform Profit Tracking
                        revenue, _ = AdminRevenue.objects.get_or_create(id=1)
                        revenue.total_withdrawals_processed += withdrawal.amount
                        revenue.save()

                        # 3. Finalize Request
                        withdrawal.status = 'Paid'
                        withdrawal.save()
                        
                        modeladmin.message_user(request, f"Approved ${withdrawal.amount} for {profile.user.username}.")
                    else:
                        messages.error(request, f"Insufficient funds: {profile.user.username}")
            except Exception as e:
                messages.error(request, f"Error: {str(e)}")

@admin.action(description='Sync/Fix Board Counts from Actual Tree')
def sync_counts_action(modeladmin, request, queryset):
    for profile in queryset:
        for i in range(1, 6):
            sync_board_count(profile, i)
    modeladmin.message_user(request, "Counts re-synchronized with actual database tree.")

@admin.register(WithdrawalRequest)
class WithdrawalRequestAdmin(admin.ModelAdmin):
    list_display = ('id','user', 'amount', 'fee', 'net_amount', 'status', 'created_at', 'wallet_address')
    list_filter = ('status', 'created_at')
    search_fields = ('user__username', 'wallet_address')
    
    # Allows editing these fields in the list view
    list_editable = ('status',) 
    
    readonly_fields = ('created_at', 'amount', 'wallet_address')
    
    # Link the action we defined above
    actions = [approve_withdrawal_action]

@admin.register(MemberProfile)
class MemberProfileAdmin(admin.ModelAdmin):
    # Use 'direct_referrals' method name instead of a count field name
    list_display = (
        'user', 'colored_status', 'full_name', 'ref_id', 'sponser',
        'direct_referrals', 'is_active', 'nfg_balance', 'balance', 'wallet', 'transaction_hash',
        'b1_display', 'b2_display', 'b3_display', 'b4_display', 'b5_display', 'view_matrix_button'
    )
    list_filter = ('current_board', 'is_active', 'payment_status')
    search_fields = ('user__username', 'ref_id', 'transaction_hash', 'sponser__user__username')
    actions = [activate_members, sync_board_count]
    list_editable = ('is_active', 'balance', 'wallet', 'nfg_balance')
    
    # --- Custom Methods for List Display ---
    def get_readonly_fields(self, request, obj=None):
        # If the position is locked and the user isn't a superuser, 
        # make the tree fields (left_child, right_child, etc.) read-only.
        if obj and obj.is_position_locked and not request.user.is_superuser:
            return ['left_child_b1', 'right_child_b1', 'sponser', 'current_board'] 
        return self.readonly_fields

    def has_delete_permission(self, request, obj=None):
        # Only superusers can delete a member profile
        return request.user.is_superuser
    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('<path:object_id>/matrix/', self.admin_site.admin_view(self.matrix_view), name='member-matrix'),
        ]
        return custom_urls + urls
     
    def direct_referrals(self, obj):
        # Counts how many people have this user as their sponsor
        return obj.referrals.count()
    direct_referrals.short_description = 'Directs'

    def colored_status(self, obj):
        colors = {'paid': '#28a745', 'pending': '#ffc107'}
        color = colors.get(obj.payment_status, '#dc3545')
        return format_html(
            '<b style="color: white; background-color: {}; padding: 3px 10px; border-radius: 10px;">{}</b>',
            color, obj.payment_status.upper()
        )
    colored_status.short_description = 'Payment Status'
    
    # admin.py snippet
    def matrix_view(self, request, object_id):
        profile = self.get_object(request, object_id)
    # Get tree data for all 5 boards
        boards_data= []

        for i in range(1, 6):
            boards_data.append(get_board_tree(profile, i))

        context = {
            'profile': profile,
            'boards': boards_data,
            'title': f"Matrix View: {profile.user.username}"
        }
        return render(request, 'matrix/matrix_tree.html', context)
    def view_matrix_button(self, obj):
        return format_html(
            '<a class="button" href="{}">View 5-Board Tree</a>',
            f'{obj.pk}/matrix/'
        )
    view_matrix_button.short_description = 'Visual Tree'

    # Shortened headers for the counts (B1-B5)
    def b1_display(self, obj): return obj.board_1_count_value
    b1_display.short_description = 'B1'

    def b2_display(self, obj): return obj.board_2_count
    b2_display.short_description = 'B2'

    def b3_display(self, obj): return obj.board_3_count
    b3_display.short_description = 'B3'

    def b4_display(self, obj): return obj.board_4_count
    b4_display.short_description = 'B4'

    def b5_display(self, obj): return obj.board_5_count
    b5_display.short_description = 'B5'

    # --- Fieldsets for Detail View ---

    fieldsets = (
        ('User Info', {
            'fields': ('user', 'full_name', 'ref_id', 'sponser', 'is_active', 'payment_status')
        }),
        ('Financials', {
            'fields': ('balance', 'wallet', 'nfg_balance', 'transaction_hash')
        }),
        ('Board Progress (6-Person Matrix)', {
            'description': 'These fields track the 2-level matrix fill for each level.',
            'fields': (
                ('board_1_count_value', 'left_child_b1', 'right_child_b1'),
                ('board_2_count', 'left_child_b2', 'right_child_b2'),
                ('board_3_count', 'left_child_b3', 'right_child_b3'),
                ('board_4_count', 'left_child_b4', 'right_child_b4'),
                ('board_5_count', 'left_child_b5', 'right_child_b5'),
            )
        }),
    )
    
@admin.action(description='Verify Payment and Place in Matrix')
def verify_and_activate(modeladmin, request, queryset):
    for profile in queryset:
        if profile.payment_status == 'PENDING':
            # 1. Update Status
            profile.payment_status = 'PAID'
            profile.is_active = True
            profile.save()
            
            # 2. Place in Matrix (Starts at Board 1)
            from .logic import place_member_with_spillover
            place_member_with_spillover(profile, profile.sponser, 1)
            
    modeladmin.message_user(request, "Selected members activated and placed in Board 1.")
# 3. Track Platform Profit
@admin.register(AdminRevenue)
class AdminRevenueAdmin(admin.ModelAdmin):
    list_display = ('total_fees_collected', 'total_withdrawals_processed', 'last_updated')
    readonly_fields = ('total_fees_collected', 'last_updated')
    fields = (
        'total_fees_collected', 
        'total_withdrawals_processed',
        ('b1_fees', 'b2_fees', 'b3_fees', 'b4_fees', 'b5_fees') # Grouped together
    )
    def has_add_permission(self, request):
        # Prevent creating multiple revenue tracking rows
        return not AdminRevenue.objects.exists()

@admin.register(MatrixNode)
class MatrixNodeAdmin(admin.ModelAdmin):
    # This helps you spot duplicates instantly
    list_display = ('user', 'board', 'parent_profile', 'position', 'created_at')
    list_filter = ('board', 'created_at', 'position')
    search_fields = ('user__username', 'parent__username')
    ordering = ('-created_at',)

    # Action to quickly remove duplicates if found
    actions = ['remove_duplicates']

    def remove_duplicates(self, request, queryset):
        # Logic to help you clean up if needed
        pass