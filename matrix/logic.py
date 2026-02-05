from collections import deque
from decimal import Decimal
from django.db import transaction
from django.db.models import Q, F
from django.contrib.auth.models import User
from .models import MemberProfile, AdminRevenue, Transaction, MatrixNode

# --- Configurations ---
BOARD_CONFIGS = {
    1: {"name": "Starter", "payout": Decimal('200.00'), "base": Decimal('50.00'), "next_fee": Decimal('150.00')},
    2: {"name": "Basic", "payout": Decimal('600.00'), "base": Decimal('150.00'), "next_fee": Decimal('400.00')},
    3: {"name": "Bronze", "payout": Decimal('1600.00'), "base": Decimal('400.00'), "next_fee": Decimal('1100.00')},
    4: {"name": "Silver", "payout": Decimal('4400.00'), "base": Decimal('1100.00'), "next_fee": Decimal('3400.00')},
    5: {"name": "Gold", "payout": Decimal('13600.00'), "base": Decimal('3400.00'), "next_fee": None},
}
FEE_RATE = Decimal('0.10')

# --- Helper Functions ---

def award_nfg_airdrop(profile, board_level):
    rewards = {1: 110, 2: 300, 3: 800, 4: 2200, 5: 6800}
    reward = rewards.get(board_level, 0)
    if reward > 0:
        MemberProfile.objects.filter(pk=profile.pk).update(nfg_balance=F('nfg_balance') + reward)
        profile.add_transaction('AIRDROP', reward, f"NFG Reward for Board {board_level} Completion")

def track_admin_fee(amount, board_level):
    stats, _ = AdminRevenue.objects.get_or_create(id=1)
    amount_dec = Decimal(str(amount))
    stats.total_fees_collected += amount_dec
    field = f'b{board_level}_fees'
    if hasattr(stats, field):
        setattr(stats, field, (getattr(stats, field) or 0) + amount_dec)
    stats.save()

def get_parent_of_member(member, board_level):
    node = MatrixNode.objects.filter(user=member.user, board=board_level).first()
    return node.parent_profile if node else None

# --- Core Logic ---

@transaction.atomic
def handle_cycle(profile, board_level):
    """Triggered when board_count reaches 6."""
    config = BOARD_CONFIGS.get(board_level)
    next_fee = config.get('next_fee')
    
    # 1. Calculate Fees
    award_nfg_airdrop(profile, board_level)
    total_earned_on_payline = config['base'] * 4 
    admin_cut = total_earned_on_payline * FEE_RATE
    track_admin_fee(admin_cut, board_level)

    # 2. Financial Update (Deduction for upgrade)
    deduction = admin_cut + (next_fee if next_fee else 0)
    
    MemberProfile.objects.filter(pk=profile.pk).update(
        balance=F('balance') - deduction,
        wallet=F('wallet') - deduction,
        cycle_count=F('cycle_count') + 1
    )
    
    
    Transaction.objects.create(
        profile=profile,
        amount=-deduction,
        tx_type='UPGRADE',
        detail=f"Board {board_level} Complete. Fee + Upgrade to Board {board_level + 1 if next_fee else board_level}"
    )
    
    # 4. Reset Current Board State
    count_attr = f'board_{board_level}_count' if board_level > 1 else 'board_1_count_value'
    MemberProfile.objects.filter(pk=profile.pk).update(**{
        count_attr: 0,
        f'left_child_b{board_level}': None,
        f'right_child_b{board_level}': None,
        'cycle_count': F('cycle_count') + 1
    })
    
    # Delete Node so user can re-enter this board level later if needed
    MatrixNode.objects.filter(user=profile.user, board=board_level).delete()
    
    profile.refresh_from_db()
    
    # 5. Move to Next Board
    if next_fee:
        profile.current_board = board_level + 1
        profile.save()
        
        # Fallback to Admin if sponsor is missing
        target_sponser = getattr(profile, 'sponser', None)
        if not target_sponser:
            admin = User.objects.filter(is_superuser=True).first()
            target_sponser = admin.memberprofile if admin else None
            
        if target_sponser:
            place_member_with_spillover(profile, target_sponser, board_level + 1)

@transaction.atomic
def place_member_with_spillover(new_member, sponser, board_level):
    """BFS Spillover Placement."""
    if MatrixNode.objects.filter(user=new_member.user, board=board_level).exists():
        return None

    left_attr = f'left_child_b{board_level}'
    right_attr = f'right_child_b{board_level}'

    queue = deque([sponser])
    target_parent, position = None, None

    while queue:
        current = queue.popleft()
        # Check Left
        l_child = getattr(current, left_attr)
        if not l_child:
            target_parent, position = current, 1
            break
        queue.append(l_child)
        # Check Right
        r_child = getattr(current, right_attr)
        if not r_child:
            target_parent, position = current, 2
            break
        queue.append(r_child)

    if target_parent:
        setattr(target_parent, (left_attr if position == 1 else right_attr), new_member)
        target_parent.save()

        MatrixNode.objects.create(
            user=new_member.user,
            board=board_level,
            parent_profile=target_parent,
            position=position
        )

        new_member.lock_position()
        update_ancestor_counts(new_member, board_level)
        return target_parent
    return None

def update_ancestor_counts(member, board_level):
    """The 2x2 Payout and Upgrade Engine."""
    config = BOARD_CONFIGS.get(board_level)
    reward_amount = config['base']
    # Match the weird naming convention in your models.py
    count_attr = f'board_{board_level}_count' if board_level > 1 else 'board_1_count_value'

    # 1. Update Parent Count
    parent = get_parent_of_member(member, board_level)
    if parent:
        MemberProfile.objects.filter(pk=parent.pk).update(**{count_attr: F(count_attr) + 1})
        parent.refresh_from_db()
        # This triggers the model-level checks for Level 1 children
        parent._check_and_cycle() 

        # 2. Update Grandparent (The person on the Payline)
        grandparent = get_parent_of_member(parent, board_level)
        if grandparent:
            MemberProfile.objects.filter(pk=grandparent.pk).update(**{count_attr: F(count_attr) + 1})
            grandparent.refresh_from_db()
            
            total_fill = getattr(grandparent, count_attr)
            
            # 3. Payline Bonus (Slots 3, 4, 5, 6)
            # We pay the grandparent because 'member' is their Level 2 (payline)
            if 3 <= total_fill <= 6:
                MemberProfile.objects.filter(pk=grandparent.pk).update(
                    wallet=F('wallet') + reward_amount,
                    balance=F('balance') + reward_amount
                )
                Transaction.objects.create(
                    profile=grandparent,
                    tx_type='CYCLE',
                    amount=reward_amount,
                    detail=f"Board {board_level} payline bonus from {member.user.username}"
                )

            # 4. Trigger the Board Cycle/Upgrade
            # This calls handle_cycle which deducts the upgrade fee and moves them
            if total_fill >= 6:
                handle_cycle(grandparent, board_level)
            else:
                # Still run this to update the visual board counts in the model
                grandparent._check_and_cycle()

def get_board_tree(profile, board_level):
    """
    Returns the visual structure of a 2x2 matrix for a user.
    """
    left_attr = f'left_child_b{board_level}'
    right_attr = f'right_child_b{board_level}'

    left_child = getattr(profile, left_attr)
    right_child = getattr(profile, right_attr)

    return {
        "level": board_level,
        "root": profile,
        "shoulders": {
            "left": left_child,
            "right": right_child,
        },
        "payline": {
            "ll": getattr(left_child, left_attr) if left_child else None,
            "lr": getattr(left_child, right_attr) if left_child else None,
            "rl": getattr(right_child, left_attr) if right_child else None,
            "rr": getattr(right_child, right_attr) if right_child else None,
        }
    }

def sync_board_count(profile, board_level):
    """
    Recalculates the count based on actual children in the database.
    """
    tree = get_board_tree(profile, board_level)
    actual_count = 0
    if tree['shoulders']['left']: actual_count += 1
    if tree['shoulders']['right']: actual_count += 1
    actual_count += sum(1 for node in tree['payline'].values() if node)
    
    count_attr = f'board_{board_level}_count' if board_level > 1 else 'board_1_count_value'
    setattr(profile, count_attr, actual_count)
    profile.save()