import datetime
import logging
import sqlite3
import pytz
import matplotlib.pyplot as plt
import io
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
    filters
)

# --- Database Setup --- #
def get_db_connection():
    conn = sqlite3.connect('budget.db', check_same_thread=False)
    conn.row_factory = sqlite3.Row  # Allows dictionary-style access
    return conn

def init_db():
    with get_db_connection() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS transactions
                     (id INTEGER PRIMARY KEY, user_id INTEGER, type TEXT, amount REAL,
                     description TEXT, date TEXT, category TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS holds
                     (id INTEGER PRIMARY KEY, user_id INTEGER, amount REAL, description TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS categories
                     (id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT, type TEXT)''')

# --- Constants --- #
MAIN_MENU, ADD_INCOME, ADD_OUTCOME, ADD_HOLD, MANAGE_HOLDS, SET_CATEGORY = range(6)
TIMEZONE = pytz.timezone('Europe/Moscow')  # Change to your timezone
DEFAULT_CATEGORIES = {
    'income': ['Salary', 'Freelance', 'Investments', 'Gifts'],
    'outcome': ['Food', 'Transport', 'Utilities', 'Entertainment', 'Shopping']
}

# Keyboard layouts
main_keyboard = [['💰 Balance', '📥 Income'], ['📤 Outcome', '⏳ Holds'], ['📊 Statistics', '🗂 Categories']]
cancel_keyboard = [['❌ Cancel']]

# --- Logging Setup --- #
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Helper Functions --- #
def format_money(amount):
    return f"${abs(amount):.2f}"

def get_current_datetime():
    return datetime.datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")

def generate_balance_chart(income, outcome, holds):
    # Create pie chart for balance breakdown
    labels = ['Income', 'Expenses', 'Holds']
    sizes = [income, -outcome, holds]
    colors = ['#4CAF50', '#F44336', '#FFC107']
    
    fig, ax = plt.subplots()
    ax.pie(sizes, labels=labels, colors=colors, autopct='%1.1f%%', startangle=90)
    ax.axis('equal')  # Equal aspect ratio ensures the pie chart is circular
    ax.set_title('Balance Breakdown')
    
    # Save plot to bytes
    buf = io.BytesIO()
    plt.savefig(buf, format='png')
    buf.seek(0)
    plt.close()
    return buf

def generate_category_chart(transactions, title):
    if not transactions:
        return None
    
    categories = [t['category'] if t['category'] else 'Uncategorized' for t in transactions]
    amounts = [abs(t['amount']) for t in transactions]
    
    # Group by category
    data = {}
    for cat, amt in zip(categories, amounts):
        data[cat] = data.get(cat, 0) + amt
    
    labels = list(data.keys())
    sizes = list(data.values())
    
    fig, ax = plt.subplots()
    ax.pie(sizes, labels=labels, autopct='%1.1f%%', startangle=90)
    ax.axis('equal')
    ax.set_title(title)
    
    buf = io.BytesIO()
    plt.savefig(buf, format='png')
    buf.seek(0)
    plt.close()
    return buf

# --- Handlers --- #
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.message.from_user
    
    # Initialize default categories for new users
    with get_db_connection() as conn:
        existing_cats = conn.execute("SELECT COUNT(*) FROM categories WHERE user_id = ?", 
                                   (user.id,)).fetchone()[0]
        if existing_cats == 0:
            for cat_type, categories in DEFAULT_CATEGORIES.items():
                for cat_name in categories:
                    conn.execute("INSERT INTO categories (user_id, name, type) VALUES (?, ?, ?)",
                               (user.id, cat_name, cat_type))
    
    await update.message.reply_text(
        f"Welcome to Budget Planner, {user.first_name}!\n"
        "Use the buttons below to manage your finances:",
        reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True)
    )
    return MAIN_MENU

async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    
    with get_db_connection() as conn:
        income = conn.execute("SELECT SUM(amount) FROM transactions WHERE user_id = ? AND type = 'income'", 
                            (user_id,)).fetchone()[0] or 0
        outcome = conn.execute("SELECT SUM(amount) FROM transactions WHERE user_id = ? AND type = 'outcome'", 
                             (user_id,)).fetchone()[0] or 0
        holds = conn.execute("SELECT SUM(amount) FROM holds WHERE user_id = ?", 
                           (user_id,)).fetchone()[0] or 0

    balance = income + outcome  # Outcome is stored as negative
    total_expenses = -outcome if outcome < 0 else outcome

    # Generate balance chart
    chart = generate_balance_chart(income, outcome, holds)
    
    # Send message with chart
    await update.message.reply_photo(
        photo=chart,
        caption=f"💰 Current Balance: {format_money(balance)}\n"
                f"📥 Total Income: {format_money(income)}\n"
                f"📤 Total Expenses: {format_money(total_expenses)}\n"
                f"⏳ Held Amount: {format_money(holds)}",
        reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
    return MAIN_MENU

async def show_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    
    with get_db_connection() as conn:
        # Get last 30 days transactions
        last_month = (datetime.datetime.now(TIMEZONE) - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
        income_trans = conn.execute("SELECT * FROM transactions WHERE user_id = ? AND type = 'income' AND date >= ?",
                                  (user_id, last_month)).fetchall()
        outcome_trans = conn.execute("SELECT * FROM transactions WHERE user_id = ? AND type = 'outcome' AND date >= ?",
                                    (user_id, last_month)).fetchall()
    
    # Generate charts
    income_chart = generate_category_chart(income_trans, "Income by Category (Last 30 Days)")
    outcome_chart = generate_category_chart(outcome_trans, "Expenses by Category (Last 30 Days)")
    
    if income_chart:
        await update.message.reply_photo(
            photo=income_chart,
            caption="Income Statistics",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
    
    if outcome_chart:
        await update.message.reply_photo(
            photo=outcome_chart,
            caption="Expense Statistics",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
    
    if not income_chart and not outcome_chart:
        await update.message.reply_text(
            "No transaction data available for the last 30 days.",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
    
    return MAIN_MENU

async def category_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [
        ["📥 Income Categories", "📤 Expense Categories"],
        ["➕ Add Category", "🔙 Back"]
    ]
    await update.message.reply_text(
        "Manage your transaction categories:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
    return MAIN_MENU

async def show_categories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    category_type = "income" if "Income" in update.message.text else "outcome"
    
    with get_db_connection() as conn:
        categories = conn.execute("SELECT name FROM categories WHERE user_id = ? AND type = ?",
                                (user_id, category_type)).fetchall()
    
    if not categories:
        await update.message.reply_text(
            f"No {category_type} categories found!",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU
    
    category_list = "\n".join([f"• {cat['name']}" for cat in categories])
    await update.message.reply_text(
        f"{category_type.capitalize()} Categories:\n{category_list}",
        reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
    return MAIN_MENU

async def add_category_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [["Income", "Outcome"], ["❌ Cancel"]]
    await update.message.reply_text(
        "Select category type:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
    return SET_CATEGORY

async def set_category_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['category_type'] = update.message.text.lower()
    await update.message.reply_text(
        "Enter the name of the new category:",
        reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True))
    return SET_CATEGORY

async def add_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    category_name = update.message.text
    category_type = context.user_data.get('category_type', 'outcome')
    
    with get_db_connection() as conn:
        conn.execute("INSERT INTO categories (user_id, name, type) VALUES (?, ?, ?)",
                   (user_id, category_name, category_type))
    
    await update.message.reply_text(
        f"✅ Added new {category_type} category: {category_name}",
        reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
    return MAIN_MENU

async def income_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    
    with get_db_connection() as conn:
        categories = conn.execute("SELECT name FROM categories WHERE user_id = ? AND type = 'income'",
                                (user_id,)).fetchall()
    
    category_list = "\n".join([f"• {cat['name']}" for cat in categories]) if categories else "No categories set"
    
    await update.message.reply_text(
        "➕ Add income in format:\n"
        "<b>Amount</b> (e.g., 1000)\n"
        "OR\n"
        "<b>Amount Description</b> (e.g., 1000 Salary)\n"
        "OR\n"
        "<b>Amount Category</b> (e.g., 1000 Freelance)\n\n"
        f"Available categories:\n{category_list}",
        reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True),
        parse_mode="HTML")
    return ADD_INCOME

async def add_income(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    text = update.message.text.split()

    try:
        amount = float(text[0])
        description = ' '.join(text[1:]) if len(text) > 1 else "Income"
        category = None
        
        # Check if the description matches a category
        with get_db_connection() as conn:
            categories = conn.execute("SELECT name FROM categories WHERE user_id = ? AND type = 'income'",
                                    (user_id,)).fetchall()
            categories = [cat['name'].lower() for cat in categories]
            
            if description.lower() in categories:
                category = description
                description = f"Income ({category})"

        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO transactions (user_id, type, amount, description, date, category) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, 'income', amount, description, get_current_datetime(), category)
            )

        await update.message.reply_text(
            f"✅ Added income: {format_money(amount)} for {description}",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU

    except (ValueError, IndexError):
        await update.message.reply_text(
            "❌ Invalid format. Please enter amount and optional description/category",
            reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True))
        return ADD_INCOME

async def outcome_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    
    with get_db_connection() as conn:
        categories = conn.execute("SELECT name FROM categories WHERE user_id = ? AND type = 'outcome'",
                                (user_id,)).fetchall()
    
    category_list = "\n".join([f"• {cat['name']}" for cat in categories]) if categories else "No categories set"
    
    await update.message.reply_text(
        "➖ Add expense in format:\n"
        "<b>Amount</b> (e.g., 50)\n"
        "OR\n"
        "<b>Amount Description</b> (e.g., 50 Groceries)\n"
        "OR\n"
        "<b>Amount Category</b> (e.g., 50 Food)\n\n"
        f"Available categories:\n{category_list}",
        reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True),
        parse_mode="HTML")
    return ADD_OUTCOME

async def add_outcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    text = update.message.text.split()

    try:
        amount = float(text[0]) * -1  # Store as negative
        description = ' '.join(text[1:]) if len(text) > 1 else "Expense"
        category = None
        
        # Check if the description matches a category
        with get_db_connection() as conn:
            categories = conn.execute("SELECT name FROM categories WHERE user_id = ? AND type = 'outcome'",
                                    (user_id,)).fetchall()
            categories = [cat['name'].lower() for cat in categories]
            
            if description.lower() in categories:
                category = description
                description = f"Expense ({category})"

        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO transactions (user_id, type, amount, description, date, category) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, 'outcome', amount, description, get_current_datetime(), category)
            )

        await update.message.reply_text(
            f"✅ Added expense: {format_money(-amount)} for {description}",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU

    except (ValueError, IndexError):
        await update.message.reply_text(
            "❌ Invalid format. Please enter amount and optional description/category",
            reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True))
        return ADD_OUTCOME

# --- Hold Functions --- #
async def holds_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    
    with get_db_connection() as conn:
        holds = conn.execute("SELECT * FROM holds WHERE user_id = ?", (user_id,)).fetchall()

    if not holds:
        await update.message.reply_text(
            "No holds found! Add a new hold:",
            reply_markup=ReplyKeyboardMarkup([['➕ Add Hold'], ['🔙 Back']], resize_keyboard=True))
        return MAIN_MENU

    holds_list = "\n".join(
        f"{idx+1}. {format_money(hold['amount'])} - {hold['description']}" 
        for idx, hold in enumerate(holds)
    )
    context.user_data['holds'] = holds

    await update.message.reply_text(
        f"⏳ Your holds:\n{holds_list}\n\nSelect an action:",
        reply_markup=ReplyKeyboardMarkup(
            [['➕ Add Hold'], ['🛠 Manage Hold'], ['🔙 Back']], 
            resize_keyboard=True
        )
    )
    return MAIN_MENU

async def add_hold_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "⏳ Add hold in format:\n"
        "<b>Amount</b> (e.g., 1000)\n"
        "OR\n"
        "<b>Amount Description</b> (e.g., 1000 Amazon)",
        reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True),
        parse_mode="HTML")
    return ADD_HOLD

async def add_hold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    text = update.message.text.split()

    try:
        amount = float(text[0])
        description = ' '.join(text[1:]) if len(text) > 1 else "Hold"

        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO holds (user_id, amount, description) VALUES (?, ?, ?)",
                (user_id, amount, description)
            )

        await update.message.reply_text(
            f"⏳ Added hold: {format_money(amount)} for {description}",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU

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
            f"{format_money(hold['amount'])} - {hold['description']}", 
            callback_data=f"hold_{hold['id']}"
        )] 
        for hold in holds
    ]

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

    await query.edit_message_text(
        text=f"Hold selected: {format_money(hold['amount'])} - {hold['description']}\nChoose action:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➡️ To Income", callback_data=f"transfer_income_{hold_id}"),
             InlineKeyboardButton("⬅️ To Outcome", callback_data=f"transfer_outcome_{hold_id}")],
            [InlineKeyboardButton("✏️ Edit", callback_data=f"edit_{hold_id}"),
             InlineKeyboardButton("❌ Remove", callback_data=f"remove_{hold_id}")],
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
        
        # Add to transactions
        transaction_type = 'income' if action == 'income' else 'outcome'
        sign = 1 if action == 'income' else -1
        conn.execute(
            "INSERT INTO transactions (user_id, type, amount, description, date) VALUES (?, ?, ?, ?, ?)",
            (user_id, transaction_type, sign * hold['amount'], f"From hold: {hold['description']}", get_current_datetime())
        )
        
        # Remove hold
        conn.execute("DELETE FROM holds WHERE id = ?", (hold_id,))

    await query.edit_message_text(
        f"✅ Transferred {format_money(hold['amount'])} to {transaction_type.capitalize()}")
    return await start_over(update, context)

async def remove_hold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    hold_id = query.data.split('_')[1]

    with get_db_connection() as conn:
        conn.execute("DELETE FROM holds WHERE id = ?", (hold_id,))

    await query.edit_message_text("✅ Hold removed successfully!")
    return await start_over(update, context)

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

def main() -> None:
    # Initialize database
    init_db()

    # Create application
    application = Application.builder().token("8017763140:AAG9PeLy2ktLG5Q6ZGjTI7B8nk7eHVSxemw").build()

    # Conversation handler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            MAIN_MENU: [
                MessageHandler(filters.Regex(r'^💰 Balance$'), show_balance),
                MessageHandler(filters.Regex(r'^📥 Income$'), income_menu),
                MessageHandler(filters.Regex(r'^📤 Outcome$'), outcome_menu),
                MessageHandler(filters.Regex(r'^⏳ Holds$'), holds_menu),
                MessageHandler(filters.Regex(r'^📊 Statistics$'), show_statistics),
                MessageHandler(filters.Regex(r'^🗂 Categories$'), category_menu),
                MessageHandler(filters.Regex(r'^➕ Add Hold$'), add_hold_prompt),
                MessageHandler(filters.Regex(r'^🛠 Manage Hold$'), manage_hold_menu),
                MessageHandler(filters.Regex(r'^📥 Income Categories$'), show_categories),
                MessageHandler(filters.Regex(r'^📤 Expense Categories$'), show_categories),
                MessageHandler(filters.Regex(r'^➕ Add Category$'), add_category_prompt),
                MessageHandler(filters.Regex(r'^🔙 Back$'), start),
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
            SET_CATEGORY: [
                MessageHandler(filters.Regex(r'^(Income|Outcome)$'), set_category_type),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_category),
                MessageHandler(filters.Regex(r'^❌ Cancel$'), cancel)
            ],
            MANAGE_HOLDS: [
                CallbackQueryHandler(hold_action, pattern=r"^hold_"),
                CallbackQueryHandler(transfer_hold, pattern=r"^transfer_(income|outcome)_"),
                CallbackQueryHandler(remove_hold, pattern=r"^remove_"),
                CallbackQueryHandler(start_over, pattern=r"^back_")
            ]
        },
        fallbacks=[CommandHandler('start', start)],
        allow_reentry=True
    )

    application.add_handler(conv_handler)
    
    # Start the Bot
    application.run_polling()

if __name__ == '__main__':
    main()
