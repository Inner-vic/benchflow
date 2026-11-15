# Unmodified pure routing function from pipeline b77ebd1c; no bot or authentication code.
def resolve_notification_target(feishu, builder_id, notify_user, notify_chat_id):
    """Prefer a real Jenkins builder; otherwise use a complete agent delegation."""
    if builder_id:
        try:
            builder_user_id = feishu.lookup_user_id(builder_id)
            if builder_user_id:
                if notify_user or notify_chat_id:
                    print('[Info] Build was triggered via Jenkins by "{}", ignoring NOTIFY_CHAT_ID/NOTIFY_USER.'.format(builder_id))
                return builder_user_id, False
        except Exception as e:
            print('[Warning] Failed to look up builder_id "{}": {}. Treating it as a non-user trigger.'.format(builder_id, e))

    if notify_user and notify_chat_id:
        try:
            user_id = feishu.resolve_user_id(notify_user)
            if user_id:
                return user_id, True
            print('[Warning] Failed to resolve NOTIFY_USER "{}". Sending group notification without a mention.'.format(notify_user))
        except Exception as e:
            print('[Warning] Failed to resolve NOTIFY_USER "{}": {}. Sending group notification without a mention.'.format(notify_user, e))
    return '', False
