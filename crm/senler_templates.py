"""Editable starter menu for Papa Sushi.

Website and operator supplied by the owner. App links, addresses and the birthday
offer checked on papasushi.ru/novokuznetsk on 2026-09-17; these are editable copies,
not a live feed. Only unchanged legacy order steps are upgraded automatically.
"""


DELIVERY_MENU_KEY = 'delivery_menu'
ORDER_TEXT = '🍣 Выберите, где удобнее сделать заказ:'
LEGACY_ORDER_TEXT = (
    '🍣 Закажите на сайте или в приложении — выбирайте удобный способ:\n\n'
    '🌐 Сайт: https://papasushi.ru/novokuznetsk\n\n'
    '📱 Приложение для iPhone: https://apps.apple.com/ru/app/id1510725657\n\n'
    '📱 Приложение для Android: https://play.google.com/store/apps/details?id=ru.dvfx.papasushi'
)
LEGACY_ORDER_BUTTONS = [{'label': '🏠 Главное меню', 'action': 'goto', 'value': 'menu'}]


def order_buttons():
    return [
        {'label': '📱 App Store', 'action': 'url', 'value': 'https://apps.apple.com/ru/app/id1510725657'},
        {'label': '📱 Google Play', 'action': 'url', 'value': 'https://play.google.com/store/apps/details?id=ru.dvfx.papasushi'},
        {'label': '🌐 Сайт', 'action': 'url', 'value': 'https://papasushi.ru/novokuznetsk'},
        {'label': '🏠 Главное меню', 'action': 'goto', 'value': 'menu'},
    ]


def upgrade_delivery_order(definition):
    """Upgrade the old one-button order step without replacing other edits."""
    for node in definition.get('nodes', []):
        if node.get('id') == 'order' and node.get('type') == 'message':
            if node.get('buttons') != LEGACY_ORDER_BUTTONS:
                return False
            node['buttons'] = order_buttons()
            if node.get('text') == LEGACY_ORDER_TEXT:
                node['text'] = ORDER_TEXT
            return True
    return False


def delivery_menu_definition():
    def button(label, target):
        return {'label': label, 'action': 'goto', 'value': target}

    def message(node_id, title, text, buttons):
        return {'id': node_id, 'type': 'message', 'title': title, 'text': text,
                'asset_id': None, 'buttons': buttons, 'next': ''}

    def back():
        return button('🏠 Главное меню', 'menu')

    nodes = [
        message('menu', 'Главное меню',
                'Привет, {имя}! Это Папа Суши 🍣\n\nЗдесь можно сделать заказ, узнать об акциях '
                'и бонусной системе, написать оператору и посмотреть наши адреса.\n\nВыберите нужный раздел 👇', [
                    button('🍣 Сделать заказ', 'order'),
                    button('🔥 Актуальные акции', 'promotions'),
                    button('🎁 Бонусная система', 'bonuses'),
                    button('💬 Связаться с оператором', 'operator'),
                    button('📍 Адреса и контакты', 'contacts'),
                ]),
        message('order', 'Сделать заказ', ORDER_TEXT, order_buttons()),
        message('promotions', 'Актуальные акции',
                '🔥 Актуальные акции\n\nВыберите предложение, чтобы узнать подробности и условия:',
                [button('🎂 Скидка 15% в день рождения', 'birthday'),
                 button('🍣 Акционные сеты', 'sale'), back()]),
        message('bonuses', 'Бонусная система',
                '🎁 Бонусная система Папа Суши\n\n'
                'Копите бонусы за заказы и оплачивайте ими до 40% стоимости заказа.\n'
                '1 бонус = 1 ₽.\n\n'
                'Процент начисления зависит от суммы заказов за последние 30 дней:\n'
                '• 1% — от 0 до 1 499 ₽\n• 3% — от 1 500 до 3 999 ₽\n• 5% — от 4 000 ₽\n\n'
                'Войдите в аккаунт на сайте, чтобы посмотреть свою бонусную карту:\n\n'
                'https://papasushi.ru/novokuznetsk/bonus_card', [
                    button('🍣 Сделать заказ', 'order'), back(),
                ]),
        message('operator', 'Связаться с оператором',
                '💬 Напишите нашему оператору — он поможет с заказом и ответит на вопросы:\n\n'
                'https://t.me/papa_sushi', [back()]),
        message('contacts', 'Адреса и контакты',
                '📍 Папа Суши — Новокузнецк\n\n'
                '• Тореза, 79\n• Строителей, 84а\n• Новобайдаевская, 2/5\n• Авиаторов, 35а\n\n'
                '☎️ Единый номер доставки: 8 (3843) 34-80-70\n\n'
                'Часы работы:\nПн–Чт и Вс: 10:15–22:10\nПт–Сб: 10:15–23:10\n\n'
                'Адреса на сайте: https://papasushi.ru/novokuznetsk/landing/rest', [back()]),
        message('birthday', 'Скидка в день рождения',
                '🎂 Скидка 15% в день рождения\n\n'
                'Действует в день рождения, за 3 дня до него и 3 дня после.\n\n'
                'Войдите в профиль на сайте, откройте настройки, укажите дату рождения и сохраните её. '
                'При выполнении условий скидка применится автоматически.\n\n'
                'Не суммируется с другими скидками и акциями. '
                'Не распространяется на категории «Дополнительно» и «Закуски».\n\n'
                'Условия на сайте: https://papasushi.ru/novokuznetsk/landing/promosale', [
                    button('🍣 Сделать заказ', 'order'), button('⬅️ К акциям', 'promotions'), back(),
                ]),
        message('sale', 'Акционные сеты',
                '🍣 Акционные сеты\n\nПосмотрите предложения в разделе «Акции» на нашем сайте. '
                'Там указаны актуальные составы и цены:\n\n'
                'https://papasushi.ru/novokuznetsk/sale', [
                    button('🍣 Сделать заказ', 'order'), button('⬅️ К акциям', 'promotions'), back(),
                ]),
    ]
    return {'entry': 'menu', 'nodes': nodes}
