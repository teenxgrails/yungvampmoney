import datetime
import logging
import sqlite3
import pytz
import matplotlib.pyplot as plt
from io import BytesIO
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
    filters,
    JobQueue
)

# --- Database Setup --- #
def get_db_connection():
    conn = sqlite3.connect('budget.db', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db_connection() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS transactions
                     (id INTEGER PRIMARY KEY, user_id INTEGER, type TEXT, amount REAL,
                     description TEXT, date TEXT, currency TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS holds
                     (id INTEGER PRIMARY KEY, user_id INTEGER, amount REAL, description TEXT, currency TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS recurring
                     (id INTEGER PRIMARY KEY, user_id INTEGER, type TEXT, amount REAL,
                     description TEXT, currency TEXT, day_of_month INTEGER,
                     is_active INTEGER DEFAULT 1)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS user_settings
                     (user_id INTEGER PRIMARY KEY, default_currency TEXT)''')

# --- Constants --- #
MAIN_MENU, ADD_INCOME, ADD_OUTCOME, ADD_HOLD, MANAGE_HOLDS, \
RECURRING_MENU, ADD_RECURRING, CURRENCY_MENU, MANAGE_TRANSACTIONS = range(9)
TIMEZONE = pytz.timezone('Europe/Moscow')
CURRENCIES = {
    'USD': '$',
    'EUR': '€',
    'CHF': 'Fr',
    'GBP': '£',
    'JPY': '¥',
    'RUB': '₽'
}

# Keyboard layouts
main_keyboard = [
    ['💰 Balance' ], 
    ['📥 Income', '📤 Outcome', ],
    ['⏳ Holds', '🔄 Recurring', '💱 Currency', '🗑 Transactions']
]
cancel_keyboard = [['❌ Cancel']]

# --- Logging Setup --- #
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Helper Functions --- #
def format_transaction_date_long(db_date):
    if not db_date:
        return "no date"
    try:
        dt = datetime.datetime.strptime(db_date, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%Y-%m-%d %H:%M")
    except:
        return db_date.split()[0] if db_date else "no date"
    
def format_transaction_date(db_date):
    if not db_date:
        return "no date"
    try:
        dt = datetime.datetime.strptime(db_date, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%b %d")
    except:
        return db_date.split()[0] if db_date else "no date"
    
def get_user_currency(user_id):
    with get_db_connection() as conn:
        currency = conn.execute("SELECT default_currency FROM user_settings WHERE user_id = ?", 
                              (user_id,)).fetchone()
        return currency['default_currency'] if currency else 'USD'

def format_money(amount, currency='USD'):
    symbol = CURRENCIES.get(currency, '$')
    
    # European-style number formatting:
    # - Comma as decimal separator
    # - Space as thousand separator
    # - 2 decimal places
    formatted_amount = "{:,.2f}".format(abs(amount)).replace(",", " ").replace(".", ",")
    
    # Handle currency symbol placement
    if currency in ['USD', 'GBP', 'JPY', 'RUB']:  # Prefix symbols
        return f"{symbol}{formatted_amount}"
    elif currency in ['EUR', 'CHF']:  # Suffix symbols
        return f"{formatted_amount}{symbol}"
    else:  # Default prefix
        return f"{symbol}{formatted_amount}"


def get_current_datetime():
    return datetime.datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")

async def show_transactions_for_deletion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    currency = get_user_currency(user_id)
    
    with get_db_connection() as conn:
        transactions = conn.execute(
            "SELECT * FROM transactions WHERE user_id = ? "
            "ORDER BY date DESC LIMIT 50",  # Limit to 50 most recent for practicality
            (user_id,)
        ).fetchall()

    if not transactions:
        await update.message.reply_text(
            "No transactions found!",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU

    keyboard = []
    for idx, t in enumerate(transactions, 1):
        date = format_transaction_date(t['date'])
        trans_type = "Income" if t['type'] == 'income' else "Expense"
        amount = t['amount'] if t['amount'] > 0 else -t['amount']
        trans_currency = t['currency'] if 'currency' in t.keys() else currency
        description = t['description'][:20] + '...' if len(t['description']) > 20 else t['description']
        
        button_text = (
            f"{idx}. {date} | {trans_type} | "
            f"{format_money(amount, trans_currency)} | {description}"
        )
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"del_trans_{t['id']}")])

    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_transactions")])

    await update.message.reply_text(
        "Select a transaction to delete:",
        reply_markup=InlineKeyboardMarkup(keyboard))
    return MANAGE_TRANSACTIONS

async def delete_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    trans_id = query.data.split('_')[2]

    with get_db_connection() as conn:
        conn.execute("DELETE FROM transactions WHERE id = ?", (trans_id,))

    await query.edit_message_text("✅ Transaction deleted successfully!")
    return await show_balance_menu(update, context)

async def process_recurring_transactions(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.datetime.now(TIMEZONE).day
    with get_db_connection() as conn:
        recurring = conn.execute("SELECT * FROM recurring WHERE day_of_month = ? AND is_active = 1", 
                               (today,)).fetchall()
        
        for transaction in recurring:
            conn.execute(
                "INSERT INTO transactions (user_id, type, amount, description, date, currency) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (transaction['user_id'], transaction['type'], transaction['amount'], 
                 transaction['description'], get_current_datetime(), transaction['currency'])
            )
            logger.info(f"Processed recurring transaction for user {transaction['user_id']}")

async def show_balance_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Display balance and return to main menu"""
    await show_balance(update, context)
    return MAIN_MENU

async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id if update.message else update.callback_query.from_user.id
    currency = get_user_currency(user_id)
    
    with get_db_connection() as conn:
        income = conn.execute("SELECT SUM(amount) FROM transactions WHERE user_id = ? AND type = 'income'", 
                            (user_id,)).fetchone()[0] or 0
        outcome = conn.execute("SELECT SUM(amount) FROM transactions WHERE user_id = ? AND type = 'outcome'", 
                             (user_id,)).fetchone()[0] or 0
        holds = conn.execute("SELECT SUM(amount) FROM holds WHERE user_id = ?", 
                           (user_id,)).fetchone()[0] or 0
        balance = income + outcome
        total_expenses = -outcome if outcome < 0 else outcome

        transactions = conn.execute(
            "SELECT * FROM transactions WHERE user_id = ? "
            "ORDER BY date DESC LIMIT 50",  # Limit to 50 for practical reasons
            (user_id,)
        ).fetchall()

    # iOS-style visualization with European numbers
    plt.style.use('default')
    fig, ax = plt.subplots(figsize=(8, 6), facecolor='#F2F2F7')
    fig.patch.set_alpha(0)
    
    ios_colors = {
        'income': '#32D74B',
        'expenses': '#FF453A',
        'holds': '#FF9F0A',
        'background': '#F2F2F7',
        'text': '#1C1C1E'
    }
    
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.grid(False)
    
    sizes = [income, total_expenses, holds]
    labels = [f'Income\n{format_money(income, currency)}', 
              f'Expenses\n{format_money(total_expenses, currency)}', 
              f'Holds\n{format_money(holds, currency)}']
    colors = [ios_colors['income'], ios_colors['expenses'], ios_colors['holds']]
    
    wedges, texts = ax.pie(sizes, colors=colors, startangle=90, 
                          wedgeprops=dict(width=0.5, edgecolor='none'))
    
    for text in texts:
        text.set_color(ios_colors['text'])
        text.set_fontsize(10)
        text.set_fontweight('medium')
    
    center_text = f"Balance\n{format_money(balance, currency)}"
    ax.text(0, 0, center_text, ha='center', va='center', 
           fontsize=18, fontweight='bold', color=ios_colors['text'])
    
    for wedge in wedges:
        wedge.set_edgecolor('#D1D1D6')
        wedge.set_linewidth(0.5)
    
    plt.tight_layout()
    
    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=120, transparent=True, 
               bbox_inches='tight', pad_inches=0.1)
    buf.seek(0)
    plt.close()
    
    # Prepare European-style formatted text
    text_summary = (
        f"💳 *Current Balance*: {format_money(balance, currency)}\n"
        f"📈 *Total Income*: {format_money(income, currency)}\n"
        f"📉 *Total Expenses*: {format_money(total_expenses, currency)}\n"
        f"⏳ *Held Amount*: {format_money(holds, currency)}\n"
        f"🌍 *Currency*: {currency} {CURRENCIES.get(currency, '')}\n\n"
    )
    
    if transactions:
        trans_history = "📜 *Transaction History*\n"
        trans_history += "` Type |  Date  |   Amount   | Description `\n"
        trans_history += "`-----------------------------------------------`\n"
        
        for t in transactions:
            date = format_transaction_date(t['date'])
            trans_type = "🟢" if t['type'] == 'income' else "🔴"
            amount = t['amount'] if t['amount'] > 0 else -t['amount']
            trans_currency = t['currency'] if 'currency' in t.keys() else currency
            description = t['description'][:20] + '...' if len(t['description']) > 20 else t['description']
            
            # Properly format each column
            trans_history += (
                f"`{trans_type:<1} | {date:<5} | "
                f"{format_money(amount, trans_currency):<7} | "
                f"{description}`\n"
            )
    else:
        trans_history = "No transactions yet"
    
    # Send messages
    if update.message:
        await update.message.reply_photo(
            photo=buf,
            caption=text_summary,
            parse_mode='Markdown',
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True)
        )
        await update.message.reply_text(
            trans_history,
            parse_mode='Markdown',
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True)
        )
    else:
        await update.callback_query.message.reply_photo(
            photo=buf,
            caption=text_summary,
            parse_mode='Markdown',
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True)
        )
        await update.callback_query.message.reply_text(
            trans_history,
            parse_mode='Markdown',
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True)
        )
    return MAIN_MENU

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.message.from_user
    
    with get_db_connection() as conn:
        user_exists = conn.execute("SELECT 1 FROM user_settings WHERE user_id = ?", 
                                 (user.id,)).fetchone()
        if not user_exists:
            conn.execute("INSERT INTO user_settings (user_id, default_currency) VALUES (?, ?)",
                       (user.id, 'USD'))
    
    await update.message.reply_text(
        f"Welcome to Budget Planner, {user.first_name}!\n"
        "Use the buttons below to manage your finances:",
        reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
    return MAIN_MENU

async def currency_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [
        [InlineKeyboardButton(f"{code} {symbol}", callback_data=f"currency_{code}") 
         for code, symbol in list(CURRENCIES.items())[i:i+2]]
        for i in range(0, len(CURRENCIES), 2)
    ]
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_currency")])
    
    await update.message.reply_text(
        "Select your default currency:",
        reply_markup=InlineKeyboardMarkup(keyboard))
    return CURRENCY_MENU

async def set_currency(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    currency_code = query.data.split('_')[1]
    user_id = query.from_user.id
    
    with get_db_connection() as conn:
        conn.execute("INSERT OR REPLACE INTO user_settings (user_id, default_currency) VALUES (?, ?)",
                   (user_id, currency_code))
    
    await query.edit_message_text(
        f"✅ Default currency set to {currency_code} {CURRENCIES.get(currency_code, '')}")
    return await show_balance_menu(update, context)

async def recurring_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [
        ["➕ Add Recurring", "📋 List Recurring"],
        ["🔙 Back"]
    ]
    await update.message.reply_text(
        "Manage recurring transactions:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
    return RECURRING_MENU

async def add_recurring_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [["Income", "Outcome"], ["❌ Cancel"]]
    await update.message.reply_text(
        "Select type of recurring transaction:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
    return ADD_RECURRING

async def add_recurring_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['recurring_type'] = update.message.text.lower()
    await update.message.reply_text(
        "Enter details in format:\n"
        "<b>Amount DayOfMonth Description</b>\n"
        "Example: 1000 15 Salary\n"
        "DayOfMonth should be between 1-31",
        reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True),
        parse_mode="HTML")
    return ADD_RECURRING

async def add_recurring(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    text = update.message.text.split()
    currency = get_user_currency(user_id)

    try:
        if 'recurring_type' not in context.user_data:
            await update.message.reply_text(
                "Please start over and select the transaction type first",
                reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True))
            return ADD_RECURRING

        amount = float(text[0])
        day = int(text[1])
        description = ' '.join(text[2:]) if len(text) > 2 else "Recurring"
        
        if not 1 <= day <= 31:
            raise ValueError("Day must be between 1-31")

        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO recurring (user_id, type, amount, description, day_of_month, currency) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, context.user_data['recurring_type'], amount, description, day, currency)
            )

        context.user_data.pop('recurring_type', None)
        return await show_balance_menu(update, context)

    except (ValueError, IndexError) as e:
        await update.message.reply_text(
            f"❌ Invalid format: {str(e)}\n"
            "Please enter: Amount DayOfMonth Description\n"
            "Example: 1000 15 Salary",
            reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True))
        return ADD_RECURRING

async def list_recurring(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    
    with get_db_connection() as conn:
        recurring = conn.execute("SELECT * FROM recurring WHERE user_id = ?", 
                              (user_id,)).fetchall()

    if not recurring:
        await update.message.reply_text(
            "No recurring transactions found!",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU

    keyboard = [
        [InlineKeyboardButton(
            f"{idx}. {trans['type'].capitalize()}: {format_money(trans['amount'], trans['currency'])} "
            f"on day {trans['day_of_month']} - {trans['description']}",
            callback_data=f"recur_{trans['id']}"
        )]
        for idx, trans in enumerate(recurring, 1)
    ]
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_recurring")])

    await update.message.reply_text(
        "📋 Your recurring transactions:\nSelect one to manage:",
        reply_markup=InlineKeyboardMarkup(keyboard))
    return RECURRING_MENU

async def manage_recurring(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    recurring_id = query.data.split('_')[1]
    context.user_data['current_recurring'] = recurring_id

    with get_db_connection() as conn:
        recurring = conn.execute("SELECT * FROM recurring WHERE id = ?", 
                              (recurring_id,)).fetchone()

    currency = recurring['currency'] if 'currency' in recurring.keys() else 'USD'
    
    await query.edit_message_text(
        text=f"Recurring transaction selected:\n"
             f"Type: {recurring['type'].capitalize()}\n"
             f"Amount: {format_money(recurring['amount'], currency)}\n"
             f"Day: {recurring['day_of_month']}\n"
             f"Description: {recurring['description']}\n"
             f"Currency: {currency} {CURRENCIES.get(currency, '')}\n\n"
             "Choose action:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Edit", callback_data=f"edit_recur_{recurring_id}"),
             InlineKeyboardButton("❌ Remove", callback_data=f"remove_recur_{recurring_id}")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_recur_list")]
        ]))
    return RECURRING_MENU

async def remove_recurring(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    recurring_id = query.data.split('_')[2]

    with get_db_connection() as conn:
        conn.execute("DELETE FROM recurring WHERE id = ?", (recurring_id,))

    await query.edit_message_text("✅ Recurring transaction removed successfully!")
    return await show_balance_menu(update, context)

async def edit_recurring_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    recurring_id = query.data.split('_')[2]
    context.user_data['editing_recurring'] = recurring_id

    with get_db_connection() as conn:
        recurring = conn.execute("SELECT * FROM recurring WHERE id = ?", 
                              (recurring_id,)).fetchone()
    
    currency = recurring['currency'] if 'currency' in recurring.keys() else 'USD'
    
    await query.edit_message_text(
        f"Editing recurring transaction:\n"
        f"Current: {format_money(recurring['amount'], currency)} on day {recurring['day_of_month']} - {recurring['description']}\n\n"
        "Enter new details in format:\n"
        "<b>Amount DayOfMonth Description</b>\n"
        "Example: 1000 15 Salary\n"
        "DayOfMonth should be between 1-31",
        parse_mode="HTML"
    )
    return ADD_RECURRING

async def edit_recurring(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    text = update.message.text.split()

    try:
        amount = float(text[0])
        day = int(text[1])
        description = ' '.join(text[2:]) if len(text) > 2 else "Recurring"
        
        if not 1 <= day <= 31:
            raise ValueError("Day must be between 1-31")

        with get_db_connection() as conn:
            conn.execute(
                "UPDATE recurring SET amount = ?, day_of_month = ?, description = ? WHERE id = ?",
                (amount, day, description, context.user_data['editing_recurring'])
            )

        context.user_data.pop('editing_recurring', None)
        await update.message.reply_text("✅ Recurring transaction updated successfully!")
        return await show_balance_menu(update, context)

    except (ValueError, IndexError) as e:
        await update.message.reply_text(
            f"❌ Invalid format: {str(e)}\n"
            "Please enter: Amount DayOfMonth Description\n"
            "Example: 1000 15 Salary",
            reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True))
        return ADD_RECURRING

async def income_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    currency = get_user_currency(user_id)
    
    await update.message.reply_text(
        f"➕ Add income in format (currency: {currency}):\n"
        "<b>Amount</b> (e.g., 1000)\n"
        "OR\n"
        "<b>Amount Description</b> (e.g., 1000 Salary)",
        reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True),
        parse_mode="HTML")
    return ADD_INCOME

async def add_income(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    text = update.message.text.split()
    currency = get_user_currency(user_id)

    try:
        amount = float(text[0])
        description = ' '.join(text[1:]) if len(text) > 1 else "Income"

        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO transactions (user_id, type, amount, description, date, currency) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, 'income', amount, description, get_current_datetime(), currency)
            )

        return await show_balance_menu(update, context)

    except (ValueError, IndexError):
        await update.message.reply_text(
            "❌ Invalid format. Please enter amount and optional description",
            reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True))
        return ADD_INCOME

async def outcome_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    currency = get_user_currency(user_id)
    
    await update.message.reply_text(
        f"➖ Add expense in format (currency: {currency}):\n"
        "<b>Amount</b> (e.g., 50)\n"
        "OR\n"
        "<b>Amount Description</b> (e.g., 50 Groceries)",
        reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True),
        parse_mode="HTML")
    return ADD_OUTCOME

async def add_outcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    text = update.message.text.split()
    currency = get_user_currency(user_id)

    try:
        amount = float(text[0])
        description = ' '.join(text[1:]) if len(text) > 1 else "Expense"

        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO transactions (user_id, type, amount, description, date, currency) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, 'outcome', -amount, description, get_current_datetime(), currency)
            )

        return await show_balance_menu(update, context)

    except (ValueError, IndexError):
        await update.message.reply_text(
            "❌ Invalid format. Please enter amount and optional description",
            reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True))
        return ADD_OUTCOME

async def holds_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    currency = get_user_currency(user_id)
    
    with get_db_connection() as conn:
        holds = conn.execute("SELECT * FROM holds WHERE user_id = ?", (user_id,)).fetchall()

    if not holds:
        await update.message.reply_text(
            f"No holds found! Add a new hold (currency: {currency}):",
            reply_markup=ReplyKeyboardMarkup([['➕ Add Hold'], ['🔙 Back']], resize_keyboard=True))
        return MAIN_MENU

    holds_list = "\n".join(
        f"{idx+1}. {format_money(hold['amount'], hold['currency'])} - {hold['description']}" 
        for idx, hold in enumerate(holds)
    )

    await update.message.reply_text(
        f"⏳ Your holds:\n{holds_list}\n\nSelect an action:",
        reply_markup=ReplyKeyboardMarkup(
            [['➕ Add Hold'], ['🛠 Manage Hold'], ['🔙 Back']], 
            resize_keyboard=True
        )
    )
    return MAIN_MENU

async def add_hold_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    currency = get_user_currency(user_id)
    await update.message.reply_text(
        f"⏳ Add hold in format (currency: {currency}):\n"
        "<b>Amount</b> (e.g., 1000)\n"
        "OR\n"
        "<b>Amount Description</b> (e.g., 1000 Amazon)",
        reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True),
        parse_mode="HTML")
    return ADD_HOLD

async def add_hold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    text = update.message.text.split()
    currency = get_user_currency(user_id)

    try:
        amount = float(text[0])
        description = ' '.join(text[1:]) if len(text) > 1 else "Hold"

        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO holds (user_id, amount, description, currency) VALUES (?, ?, ?, ?)",
                (user_id, amount, description, currency)
            )

        return await show_balance_menu(update, context)

    except (ValueError, IndexError):
        await update.message.reply_text(
            "❌ Invalid format. Please enter amount and optional description",
            reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True))
        return ADD_HOLD

async def manage_hold_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    
    with get_db_connection() as conn:
        holds = conn.execute("SELECT * FROM holds WHERE user_id = ?", (user_id,)).fetchall()

    if not holds:
        await update.message.reply_text(
            "No holds available!",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU

    keyboard = [
        [InlineKeyboardButton(
            f"{format_money(hold['amount'], hold['currency'])} - {hold['description']}", 
            callback_data=f"hold_{hold['id']}"
        )] 
        for hold in holds
    ]
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_holds")])

    await update.message.reply_text(
        "Select a hold to manage:",
        reply_markup=InlineKeyboardMarkup(keyboard))
    return MANAGE_HOLDS

async def hold_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    hold_id = query.data.split('_')[1]
    context.user_data['current_hold'] = hold_id

    with get_db_connection() as conn:
        hold = conn.execute("SELECT * FROM holds WHERE id = ?", (hold_id,)).fetchone()

    currency = hold['currency'] if 'currency' in hold.keys() else 'USD'
    
    await query.edit_message_text(
        text=f"Hold selected: {format_money(hold['amount'], currency)} - {hold['description']}\nChoose action:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➡️ To Income", callback_data=f"transfer_income_{hold_id}"),
             InlineKeyboardButton("⬅️ To Expense", callback_data=f"transfer_outcome_{hold_id}")],
            [InlineKeyboardButton("❌ Remove", callback_data=f"remove_{hold_id}")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_holds")]
        ]))
    return MANAGE_HOLDS

async def transfer_hold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action, hold_id = query.data.split('_')[1], query.data.split('_')[2]
    user_id = query.from_user.id

    with get_db_connection() as conn:
        hold = conn.execute("SELECT * FROM holds WHERE id = ?", (hold_id,)).fetchone()
        currency = hold['currency'] if 'currency' in hold.keys() else 'USD'
        
        # Create transaction
        trans_type = 'income' if action == 'income' else 'outcome'
        amount = hold['amount'] if action == 'income' else -hold['amount']
        description = f"{'Released' if action == 'income' else 'Spent'} hold: {hold['description']}"
        
        conn.execute(
            "INSERT INTO transactions (user_id, type, amount, description, date, currency) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, trans_type, amount, description, get_current_datetime(), currency)
        )
        
        # Remove hold
        conn.execute("DELETE FROM holds WHERE id = ?", (hold_id,))

    return await show_balance_menu(update, context)

async def remove_hold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    hold_id = query.data.split('_')[1]

    with get_db_connection() as conn:
        conn.execute("DELETE FROM holds WHERE id = ?", (hold_id,))

    return await show_balance_menu(update, context)

async def start_over(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    reply_text = "Back to main menu:"
    reply_markup = ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True)
    
    if update.callback_query:
        await update.callback_query.message.reply_text(reply_text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(reply_text, reply_markup=reply_markup)
    return MAIN_MENU

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Action cancelled",
        reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
    return MAIN_MENU

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors caused by updates."""
    logger.error('Update "%s" caused error "%s"', update, context.error)
    if update.effective_message:
        await update.effective_message.reply_text(
            "An error occurred. Please try again.",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True)
        )

def main() -> None:
    # Initialize database
    init_db()

    # Create application with job queue
    try:
        # IMPORTANT: Replace with your actual token or use environment variables
        TOKEN = "8017763140:AAHD8fI9orJbnIyljuWF8PZN6yBmDxKuPrw"
        application = Application.builder().token(TOKEN).build()
    except Exception as e:
        logger.error(f"Failed to create application: {e}")
        return

    application.add_error_handler(error_handler)

    # Add job queue for recurring transactions
    job_queue = application.job_queue
    if job_queue:
        job_queue.run_daily(process_recurring_transactions, 
                          time=datetime.time(hour=0, minute=0, tzinfo=TIMEZONE))

    # Conversation handler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            MAIN_MENU: [
                MessageHandler(filters.Regex(r'^💰 Balance$'), show_balance_menu),
                MessageHandler(filters.Regex(r'^📥 Income$'), income_menu),
                MessageHandler(filters.Regex(r'^📤 Outcome$'), outcome_menu),
                MessageHandler(filters.Regex(r'^⏳ Holds$'), holds_menu),
                MessageHandler(filters.Regex(r'^🔄 Recurring$'), recurring_menu),
                MessageHandler(filters.Regex(r'^💱 Currency$'), currency_menu),
                MessageHandler(filters.Regex(r'^➕ Add Hold$'), add_hold_prompt),
                MessageHandler(filters.Regex(r'^🛠 Manage Hold$'), manage_hold_menu),
                MessageHandler(filters.Regex(r'^📋 List Recurring$'), list_recurring),
                MessageHandler(filters.Regex(r'^🔙 Back$'), start_over),
                MessageHandler(filters.Regex(r'^🗑 Transactions$'), show_transactions_for_deletion),
            ],
            ADD_INCOME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_income),
                MessageHandler(filters.Regex(r'^❌ Cancel$'), cancel)
            ],
            ADD_OUTCOME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_outcome),
                MessageHandler(filters.Regex(r'^❌ Cancel$'), cancel)
            ],
            ADD_HOLD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_hold),
                MessageHandler(filters.Regex(r'^❌ Cancel$'), cancel)
            ],
            RECURRING_MENU: [
                MessageHandler(filters.Regex(r'^➕ Add Recurring$'), add_recurring_prompt),
                MessageHandler(filters.Regex(r'^📋 List Recurring$'), list_recurring),
                MessageHandler(filters.Regex(r'^🔙 Back$'), start_over),
                CallbackQueryHandler(manage_recurring, pattern=r"^recur_"),
                CallbackQueryHandler(remove_recurring, pattern=r"^remove_recur_"),
                CallbackQueryHandler(edit_recurring_prompt, pattern=r"^edit_recur_"),
                CallbackQueryHandler(start_over, pattern=r"^back_recur_list"),
                CallbackQueryHandler(start_over, pattern=r"^back_recurring"),
            ],
            ADD_RECURRING: [
                MessageHandler(filters.Regex(r'^(Income|Outcome)$'), add_recurring_type),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_recurring),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_recurring),
                MessageHandler(filters.Regex(r'^❌ Cancel$'), cancel)
            ],
            CURRENCY_MENU: [
                CallbackQueryHandler(set_currency, pattern=r"^currency_"),
                CallbackQueryHandler(start_over, pattern=r"^back_currency")
            ],
            MANAGE_HOLDS: [
                CallbackQueryHandler(hold_action, pattern=r"^hold_"),
                CallbackQueryHandler(transfer_hold, pattern=r"^transfer_(income|outcome)_"),
                CallbackQueryHandler(remove_hold, pattern=r"^remove_"),
                CallbackQueryHandler(start_over, pattern=r"^back_holds")
            ],
            MANAGE_TRANSACTIONS: [
    CallbackQueryHandler(delete_transaction, pattern=r"^del_trans_"),
    CallbackQueryHandler(start_over, pattern=r"^back_transactions")
]
        },
        fallbacks=[CommandHandler('start', start)],
        allow_reentry=True
    )

    application.add_handler(conv_handler)
    
    # Start the Bot
    try:
        application.run_polling()
    except Exception as e:
        logger.error(f"Bot failed to start: {e}")

if __name__ == '__main__':
    main()
