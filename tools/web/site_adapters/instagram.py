"""Instagram landmark selectors.

Instagram's DM and modal surfaces hash CSS classes and drop name/id attributes,
so generic `inspect_interactives` often returns div-soup. These landmark hints
give the model named targets it can aim for. Selectors are kept short and
aria-driven so they survive most layout shuffles.
"""


LANDMARKS: list[dict] = [
    {
        'landmark': 'dm_inbox_link',
        'purpose': 'Open the direct-messages inbox from the main nav.',
        'suggested_selector': "a[href='/direct/inbox/']",
    },
    {
        'landmark': 'dm_thread_row',
        'purpose': 'A row in the DM thread list. Multiple matches — pair with :has-text() for a specific user.',
        'suggested_selector': "div[role='listitem']",
    },
    {
        'landmark': 'dm_message_input',
        'purpose': 'The message composer at the bottom of an open DM thread.',
        'suggested_selector': "div[role='textbox'][contenteditable='true']",
    },
    {
        'landmark': 'dm_send_button',
        'purpose': 'Send the composed DM. Usually only appears once text has been entered.',
        'suggested_selector': "div[role='button']:has-text('Send')",
    },
    {
        'landmark': 'notification_not_now',
        'purpose': 'Dismisses the "Turn on Notifications" modal that blocks navigation.',
        'suggested_selector': "button:has-text('Not Now')",
    },
    {
        'landmark': 'save_login_not_now',
        'purpose': 'Dismisses the "Save Your Login Info?" modal shown after signing in.',
        'suggested_selector': "button:has-text('Not now')",
    },
    {
        'landmark': 'profile_edit_link',
        'purpose': 'Navigate to profile edit for bio/avatar changes.',
        'suggested_selector': "a[href='/accounts/edit/']",
    },
]
