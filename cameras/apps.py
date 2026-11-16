from django.apps import AppConfig


class BdlCamerasConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'cameras'

    def ready(self):
        from . import events, popups, views

        if events.should_start():
            events.start()
            popups.start()
            views.start_stream_keeper()
