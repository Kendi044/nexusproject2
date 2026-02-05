from django.db import models
from django.contrib.auth.models import User
import uuid
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from decimal import Decimal
from django.db.models import F, Q
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.db.models.signals import post_save
from django.dispatch import receiver

class MemberProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    full_name = models.CharField(max_length=255)
    balance = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    ref_id = models.CharField(max_length=12, unique=True, blank=True)
    sponser = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='referrals')
    is_position_locked = models.BooleanField(default=False)
    wallet = models.DecimalField(max_digits=12, decimal_places=2, default=0.0)
    nfg_balance = models.DecimalField(max_digits=20, decimal_places=2, default=0.0)
    is_active = models.BooleanField(default=False)
    paid_referrals_count = models.IntegerField(default=0)
    payment_status = models.CharField(max_length=20, choices=[('pending', 'Pending'), ('paid', 'Paid')], default='pending')
    transaction_hash = models.CharField(max_length=100, unique=True, blank=True, null=True)
    is_already_placed_in_b1 = models.BooleanField(default=False)
    
    # Board Tracking
    current_board = models.IntegerField(default=1) 
    board_1_count_value = models.IntegerField(default=0, db_column='board_1_count')
    board_2_count = models.IntegerField(default=0)
    board_3_count = models.IntegerField(default=0)
    board_4_count = models.IntegerField(default=0)
    board_5_count = models.IntegerField(default=0)
    cycle_count = models.IntegerField(default=0)

    # --- Binary Connections (Keeping all 5 boards as requested) ---
    left_child_b1 = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='parent_l_b1')
    right_child_b1 = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='parent_r_b1')
    # ... (Include b2 through b5 here just as you had them)
    left_child_b2 = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='parent_l_b2')
    right_child_b2 = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='parent_r_b2')
    
    left_child_b3 = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='parent_l_b3')
    right_child_b3 = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='parent_r_b3')
    
    left_child_b4 = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='parent_l_b4')
    right_child_b4 = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='parent_r_b4')
    
    left_child_b5 = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='parent_l_b5')
    right_child_b5 = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='parent_r_b5')
    payment_order_id = models.CharField(max_length=100, blank=True, null=True)
    
    # Financial tracking per board
    board_1_earned = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    board_2_earned = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    board_3_earned = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    board_4_earned = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    board_5_earned = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)

    # --- Your Logic Methods (KEEPING THESE) ---
    def lock_position(self):
        self.is_position_locked = True
        self.save()

    def add_reward_if_eligible(self):
        """Your logic to pay for the 3rd, 4th, 5th, and 6th person."""
        cb = self.current_board
        count_field = 'board_1_count_value' if cb == 1 else f'board_{cb}_count'
        total_fill = getattr(self, count_field, 0)

        REWARD_PER_PERSON = {
            1: Decimal('50.00'), 2: Decimal('150.00'), 3: Decimal('400.00'),
            4: Decimal('1100.00'), 5: Decimal('3400.00'),
        }
        reward_amount = REWARD_PER_PERSON.get(cb, Decimal('0.00'))

        if total_fill > 2:
            eligible_people = total_fill - 2
            new_payouts_due = eligible_people - self.paid_referrals_count
            
            if self.paid_referrals_count + new_payouts_due > 4:
                new_payouts_due = 4 - self.paid_referrals_count

            if new_payouts_due > 0:
                total_reward = reward_amount * new_payouts_due
                # Update using F() to be thread-safe
                MemberProfile.objects.filter(pk=self.pk).update(
                    wallet=F('wallet') + total_reward,
                    balance=F('balance') + total_reward,
                    paid_referrals_count=F('paid_referrals_count') + new_payouts_due
                )
                self.add_transaction('CYCLE', total_reward, f"Level 2 Reward - Board {cb}")

    def _check_and_cycle(self):
        BOARD_CONFIG = {
            1: {'fee': Decimal('50.00'), 'field': 'board_1_count_value'},
            2: {'fee': Decimal('150.00'), 'field': 'board_2_count'},
            3: {'fee': Decimal('400.00'), 'field': 'board_3_count'},
            4: {'fee': Decimal('1100.00'), 'field': 'board_4_count'},
            5: {'fee': Decimal('3400.00'), 'field': 'board_5_count'},
        }

        cb = self.current_board
        if cb not in BOARD_CONFIG:
            return

        # 1. CALCULATE total_fill (The "Brain" of the matrix)
        left = getattr(self, f'left_child_b{cb}', None)
        right = getattr(self, f'right_child_b{cb}', None)

        l1 = (1 if left else 0) + (1 if right else 0)
        l2 = 0
        for child in [left, right]:
            if child:
                # We check the slots of the children to find the "Payline" (Level 2)
                l2 += (1 if getattr(child, f'left_child_b{cb}', None) else 0)
                l2 += (1 if getattr(child, f'right_child_b{cb}', None) else 0)

        # NOW total_fill is defined for the rest of the function
        total_fill = l1 + l2

        # 2. UPDATE the count in the database
        conf = BOARD_CONFIG[cb]
        MemberProfile.objects.filter(pk=self.pk).update(**{conf['field']: total_fill})
        self.refresh_from_db()


        # 4. AUTO-UPGRADE (Uses the total_fill we just calculated)
        if total_fill >= 6 and cb < 5:
            next_board = cb + 1
            upgrade_fee = BOARD_CONFIG[next_board]['fee']
                
            self.current_board = next_board
            self.paid_referrals_count = 0 
            
            # Use F() to subtract so we don't accidentally ignore the rewards added just above
            MemberProfile.objects.filter(pk=self.pk).update(
                wallet=F('wallet') - upgrade_fee,
                balance=F('balance') - upgrade_fee,
                current_board=next_board,
                paid_referrals_count=0
            )
            self.refresh_from_db()
            
            AdminRevenue.update_revenue(upgrade_fee, next_board)
            self.add_transaction('UPGRADE', -upgrade_fee, f"Upgraded to Board {next_board}")
                
                # Re-trigger placement for the new board level
                # This ensures they show up in their sponsor's Board 2, 3, etc.
            if self.sponser:
                from .logic import place_member_with_spillover
                place_member_with_spillover(self, self.sponser, next_board)


    def save(self, *args, **kwargs):
        """Clean save method without the matrix loop."""
        if not self.ref_id:
            self.ref_id = uuid.uuid4().hex[:10].upper()
        if not self.payment_order_id:
            self.payment_order_id = f"PAY-{uuid.uuid4().hex[:8].upper()}"
            
        # Ensure status sync
        if self.is_active and self.payment_status == 'pending':
            self.payment_status = 'paid'
        
        super().save(*args, **kwargs)
    
    def place_in_matrix(self, board_num):
        """Finds the first available slot in the sponsor's 2x2 matrix for a specific board."""
        if not self.sponser:
            return

        target = self.sponser
        left_attr = f'left_child_b{board_num}'
        right_attr = f'right_child_b{board_num}'

        with transaction.atomic():
            # Level 1: Check sponsor's direct left/right
            if not getattr(target, left_attr):
                MemberProfile.objects.filter(pk=target.pk).update(**{left_attr: self})
            elif not getattr(target, right_attr):
                MemberProfile.objects.filter(pk=target.pk).update(**{right_attr: self})
            
            # Level 2: Spillover into sponsor's children's slots
            else:
                lc = getattr(target, left_attr)
                rc = getattr(target, right_attr)
                placed = False
                for child in [lc, rc]:
                    if child and not placed:
                        if not getattr(child, left_attr):
                            MemberProfile.objects.filter(pk=child.pk).update(**{left_attr: self})
                            placed = True
                        elif not getattr(child, right_attr):
                            MemberProfile.objects.filter(pk=child.pk).update(**{right_attr: self})
                            placed = True

            # 2. Update Counts & Payouts for the Uplines
            target.refresh_from_db()
            target._check_and_cycle()

            if target.sponser:
                grand_target = target.sponser
                grand_target.refresh_from_db()
                grand_target._check_and_cycle()

    @property
    def available_balance(self):
        from django.db.models import Sum
        # Aggregate returns a dict, e.g., {'amount__sum': 50.00}
        pending = WithdrawalRequest.objects.filter(
            user=self.user, 
            status='Pending'
        ).aggregate(Sum('amount'))['amount__sum'] or Decimal('0.00')
    
        return self.balance - pending
                
    def add_transaction(self, tx_type, amount, detail=""):
        Transaction.objects.create(profile=self, tx_type=tx_type, amount=amount, detail=detail)  
    
    def __str__(self):
        return f"{self.full_name} ({self.ref_id})"
    
@receiver(post_save, sender=MemberProfile)
def handle_new_paid_member(sender, instance, created, **kwargs):
    # Only run if they just became 'paid' and aren't in the matrix yet
    if instance.payment_status == 'paid' and not instance.is_already_placed_in_b1:
       # Mark as placed so this signal doesn't run again on next save
        MemberProfile.objects.filter(pk=instance.pk).update(is_already_placed_in_b1=True)
        instance.refresh_from_db()
        instance.place_in_matrix(board_num=1)

class Transaction(models.Model):
    TX_TYPES = (('AIRDROP', 'Airdrop'), ('CYCLE', 'Cycle Payout'), ('UPGRADE', 'Upgrade'), ('DEBIT', 'Deduction'), ('WITHDRAWAL', 'Withdrawal'))
    profile = models.ForeignKey(MemberProfile, on_delete=models.CASCADE)
    tx_type = models.CharField(max_length=20, choices=TX_TYPES)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    detail = models.CharField(max_length=255)
    timestamp = models.DateTimeField(auto_now_add=True)

class AdminRevenue(models.Model):
    total_fees_collected = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    b1_fees = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    b2_fees = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    b3_fees = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    b4_fees = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    b5_fees = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    total_withdrawals_processed = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    last_updated = models.DateTimeField(auto_now=True)
   
    @classmethod
    def update_revenue(cls, amount, board_level):
        """Helper to update admin stats without manually fetching the record."""
        # Usually, there's only one row for AdminRevenue (pk=1)
        revenue, created = cls.objects.get_or_create(pk=1)
        
        fee_field = f'b{board_level}_fees'
        # Use F() expressions to prevent conflicts
        setattr(revenue, 'total_fees_collected', F('total_fees_collected') + amount)
        setattr(revenue, fee_field, F(fee_field) + amount)
        
        revenue.save()

    def __str__(self):
        return f"Total Revenue: ${self.total_fees_collected}"   

class WithdrawalRequest(models.Model):
    STATUS_CHOICES = [
        ('Pending', 'Pending'),
        ('Paid', 'Paid'),
        ('Cancelled', 'Cancelled'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    net_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    # FIX: Changed max_digits to max_length
    wallet_address = models.CharField(max_length=255) 
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Pending')
    # FIX: Changed auto_auto_now_add to auto_now_add
    created_at = models.DateTimeField(auto_now_add=True)
    
    WITHDRAWAL_FEE_PERCENT = Decimal('10.0')  # example: 10%

    def save(self, *args, **kwargs):
        if self.amount:
            self.fee = (self.amount * self.WITHDRAWAL_FEE_PERCENT) / 100
            self.net_amount = self.amount - self.fee
        
        if self.pk:
            try:  
                old_status = WithdrawalRequest.objects.get(pk=self.pk).status
                if old_status == 'Pending' and self.status == 'Paid':
                    profile = self.user.memberprofile
                    profile.balance -= self.amount
                    profile.wallet -= self.amount
                    profile.save()
                    profile.add_transaction('WITHDRAWAL', -self.amount, f"Withdrawal Paid")
                    
                    revenue, _ = AdminRevenue.objects.get_or_create(pk=1)
                    revenue.total_withdrawals_processed = F('total_withdrawals_processed') + self.amount
                    revenue.save()
            except WithdrawalRequest.DoesNotExist:
                pass

        # IMPORTANT: Keep this here so it runs for BOTH new and old records
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user.username} - {self.amount}"
    
@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
   if created:
      MemberProfile.objects.get_or_create(user=instance)

@receiver(post_delete, sender=MemberProfile)
def cleanup_matrix_on_delete(sender, instance, **kwargs):
    """Removes deleted member from all 5 board slots safely."""
    for i in range(1, 6):
        left_attr = f'left_child_b{i}'
        right_attr = f'right_child_b{i}'
        
        # 1. Clear the slots using .update() to avoid triggering post_save signals
        # We look for any profile that has the deleted instance in their left or right slot
        MemberProfile.objects.filter(**{left_attr: instance}).update(**{left_attr: None})
        MemberProfile.objects.filter(**{right_attr: instance}).update(**{right_attr: None})

        # 2. Identify parents who need their counts recalculated
        # We fetch them now to run the logic method
        affected_parents = MemberProfile.objects.filter(
            Q(**{left_attr: instance}) | Q(**{right_attr: instance})
        )

        for parent in affected_parents:
            # We don't need parent.save() because update() handled the DB change
            parent.refresh_from_db()
            parent._check_and_cycle()

class MatrixNode(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='matrix_positions')
    board = models.IntegerField()  # 1, 2, 3, 4, or 5
    parent_profile = models.ForeignKey(
        'MemberProfile', 
        on_delete=models.CASCADE, 
        null=True, 
        blank=True, 
        related_name='node_children'
    )
    position = models.IntegerField(choices=[(1, 'Left'), (2, 'Right')])
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} - Board {self.board} ({'Left' if self.position == 1 else 'Right'})"            