from django.db import models
from django.core import urlresolvers
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes import generic
from django.utils.translation import ugettext_lazy as _, ungettext
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.contrib.sites.models import Site
from django.utils.encoding import force_unicode

from flag import settings as flag_settings
from flag import signals
from flag.exceptions import *
from flag.utils import get_content_type_tuple


class FlaggedContentManager(models.Manager):
    """
    Manager for the FlaggedContent models
    """

    def get_for_object(self, content_object):
        """
        Helper to get a FlaggedContent instance for the given object
        """
        content_type = ContentType.objects.get_for_model(content_object)
        return self.get(content_type__id=content_type.id,
                        object_id=content_object.id)

    def filter_for_model(self, model, only_object_ids=False):
        """
        Return a queryset to filter FlaggedContent on a given model
        If `only_object_ids` is True, the queryset will only returns a list of
        object ids of the `model` model. It's usefull if the flagged model can
        not have a GenericRelation (if you can't touch the model,
        like auth.User) :
            User.objects.filter(id__in=FlaggedContent.objects.filter_for_model(
                User, True).filter(status=2))
        """
        app_label, model = get_content_type_tuple(model)
        queryset = self.filter(content_type__app_label=app_label,
                content_type__model=model)
        if only_object_ids:
            queryset = queryset.values_list('object_id', flat=True)
        return queryset

    def get_or_create_for_object(self,
                                 content_object,
                                 content_creator=None,
                                 status=None):
        """
        A wrapper around get_or_create to easily manage the fields
        `content_creator` and `status` are only set when creating the object
        """
        defaults = {}
        if content_creator is not None:
            defaults['creator'] = content_creator
        if status is not None:
            defaults['status'] = status
        flagged_content, created = FlaggedContent.objects.get_or_create(
            content_type=ContentType.objects.get_for_model(content_object),
            object_id=content_object.id,
            defaults=defaults)
        return flagged_content, created

    def model_can_be_flagged(self, content_type):
        """
        Return True if the model is listed in the MODELS settings (or if this
        settings is not defined)
        See `utils.get_content_type_tuple` for description of the
        `content_Type` parameter
        """
        if flag_settings.MODELS is None:
            return True

        # try to find app and model from the content_type
        try:
            app_label, model = get_content_type_tuple(content_type)
        except:
            return False

        # finally we can check
        model = '%s.%s' % (app_label, model)
        return model in flag_settings.MODELS

    def assert_model_can_be_flagged(self, content_type):
        """
        Raise an acception if the "model_can_be_flagged" method return False
        """
        if not self.model_can_be_flagged(content_type):
            raise ModelCannotBeFlaggedException(
                    _('This model cannot be flagged'))


class FlaggedContent(models.Model):

    content_type = models.ForeignKey(ContentType)
    object_id = models.PositiveIntegerField()
    content_object = generic.GenericForeignKey("content_type", "object_id")

    # user who created flagged content -- this is kept in model so it outlives
    # content
    creator = models.ForeignKey(User,
                                related_name="flagged_content",
                                null=True,
                                blank=True)
    status = models.PositiveSmallIntegerField(default=1, db_index=True)
    # moderator responsible for last status change
    moderator = models.ForeignKey(User,
                                  null=True,
                                  related_name="moderated_content")
    count = models.PositiveIntegerField(default=0)
    when_updated = models.DateTimeField(auto_now=True, auto_now_add=True)

    # manager
    objects = FlaggedContentManager()

    class Meta:
        unique_together = [("content_type", "object_id")]
        ordering = ('-id',)

    def __unicode__(self):
        """
        Show the flagged object in the unicode string
        """
        app_label, model = get_content_type_tuple(self.content_object)
        return u'%s.%s #%s' % (app_label, model, self.object_id)

    def content_settings(self, name):
        """
        Return the settings `name` for the current content object
        """
        return flag_settings.get_for_model(self.content_object, name)

    def count_flags_by_user(self, user):
        """
        Helper to get the number of flags on this flagged content by the
        given user
        """
        return self.flag_instances.filter(user=user, status=1).count()

    def can_be_flagged(self):
        """
        Check that the LIMIT_FOR_OBJECT is not raised
        """
        limit = self.content_settings('LIMIT_FOR_OBJECT')
        if not limit:
            return True
        return self.count < limit

    def assert_can_be_flagged(self):
        """
        Raise an acception if the "can_be_flagged" method return False
        """
        if not self.can_be_flagged():
            raise ContentFlaggedEnoughException(_('Flag limit raised'))

    def can_be_flagged_by_user(self, user):
        """
        Check that the LIMIT_SAME_OBJECT_FOR_USER is not raised for this user
        """
        if not self.can_be_flagged():
            return False
        limit = self.content_settings('LIMIT_SAME_OBJECT_FOR_USER')
        if not limit:
            return True
        return self.count_flags_by_user(user) < limit

    def assert_can_be_flagged_by_user(self, user):
        """
        Raise an exception if the given user cannot flag this object
        """
        try:
            self.assert_can_be_flagged()
        except ContentFlaggedEnoughException, e:
            raise e
        else:
            # do not use self.can_be_flagged_by_user because we need the count
            limit = self.content_settings('LIMIT_SAME_OBJECT_FOR_USER')
            if not limit:
                return
            count = self.count_flags_by_user(user)
            if count >= limit:
                error = ungettext(
                            'You already flagged this',
                            'You already flagged this %(count)d times',
                            count) % {'count': count}
                raise ContentAlreadyFlaggedByUserException(error)

    def get_content_object_admin_url(self):
        """
        Return the admin url for the content object
        """
        url = None
        if self.content_object:
            try:
                url = urlresolvers.reverse("admin:%s_%s_change" % (
                        self.content_object._meta.app_label,
                        self.content_object._meta.module_name),
                    args=(self.object_id,))
            except urlresolvers.NoReverseMatch:
                pass
        return url

    def get_content_object_absolute_url(self):
        """
        Return the absolute url for the content object
        """
        url = None
        if self.content_object:
            try:
                url = self.content_object.get_absolute_url()
            except (AttributeError,  urlresolvers.NoReverseMatch):
                pass
        return url

    def get_creator_admin_url(self):
        """
        Return the admin url for the content object's creator
        """
        url = None
        if self.creator:
            try:
                url = urlresolvers.reverse("admin:auth_user_change",
                    args=(self.creator_id,))
            except urlresolvers.NoReverseMatch:
                pass
        return url

    def get_creator_absolute_url(self):
        """
        Return the absolute url for the content object's creator
        """
        url = None
        if self.creator:
            try:
                url = User.objects.get(id=self.creator_id).get_absolute_url()
            except (AttributeError,  urlresolvers.NoReverseMatch):
                pass
        return url

    def save(self, *args, **kwargs):
        """
        Before the save, we check that we can flag this object
        """

        # check if we can flag this model
        FlaggedContent.objects.assert_model_can_be_flagged(self.content_object)

        super(FlaggedContent, self).save(*args, **kwargs)

    def flag_added(self, flag_instance, send_signal=False, send_mails=False):
        """
        Called when a flag is added, to update the count and send a signal
        """
        # increment the count if status == 1
        if self.status == 1:
            self.count = models.F('count') + 1
            self.save()

            # update count of the current object
            new_self = FlaggedContent.objects.get(id=self.id)
            self.count = new_self.count

        # send a signal if wanted
        if send_signal:
            signals.content_flagged.send(
                sender=FlaggedContent,
                flagged_content=self,
                flagged_instance=flag_instance)

        # send emails if wanted
        if send_mails and self.content_settings('SEND_MAILS'):

            # always send mail if the max flag is reached
            limit = self.content_settings('LIMIT_FOR_OBJECT')
            really_send_mails = limit \
                and self.count >= limit

            # limit not reached, check rules
            if not really_send_mails:
                # check rule
                current_min_count, current_step = 0, 0
                for min_count, step in self.content_settings(
                        'SEND_MAILS_RULES'):
                    if self.count >= min_count:
                        current_min_count, current_step = min_count, step
                    else:
                        break

                # do we need to send mail ?
                if current_step and \
                        not (self.count - current_min_count) % current_step:
                    really_send_mails = True

            # finally send mails if we really want to do it
            if really_send_mails:
                flag_instance.send_mails()

    def get_status_display(self):
        """
        Return the displayable value for the current status
        (replace the original get_FIELD_display for this field which act as a
        field with choices)
        """
        statuses = dict(flag_settings.get_for_model(self.content_object,
                                                    'STATUSES'))
        return force_unicode(statuses[self.status], strings_only=True)


class FlagInstanceManager(models.Manager):
    """
    Manager for the FlagInstance model, adding a `add` method
    """

    def add(self, user, content_object, content_creator=None, comment=None,
            status=None, send_signal=False, send_mails=False):
        """
        Helper to easily create a flag of an object
        `content_creator` can only be set if it's the first flag
        if `status` is updated, no signal/mails will be sent (update by staff)
        TODO : move things in the `save` method of the `FlagInstance` model
        """

        # get or create the FlaggedContent object
        flagged_content, created = FlaggedContent.objects.\
                get_or_create_for_object(content_object,
                                         content_creator,
                                         status)

        # save new status, moderator and updated date
        if status:
            flagged_content.status = status
            # if the status is not the default one, we save the moderator
            if status != flag_settings.STATUSES[0][0]:
                flagged_content.moderator = user
        # always update the `when_updated` field
        flagged_content.save()

        # add the flag
        params = dict(
            flagged_content=flagged_content,
            user=user,
            comment=comment)
        if status:
            params['status'] = status
        else:
            params['status'] = flagged_content.status

        flag_instance = FlagInstance(**params)
        flag_instance.save(send_signal=send_signal,
                           send_mails=send_mails)

        return flag_instance


class FlagInstance(models.Model):

    flagged_content = models.ForeignKey(FlaggedContent, related_name='flag_instances')
    user = models.ForeignKey(User)  # user flagging the content
    when_added = models.DateTimeField(auto_now=False, auto_now_add=True)
    comment = models.TextField(null=True, blank=True)  # comment by the flagger
    status = models.PositiveSmallIntegerField(default=1, db_index=True)

    objects = FlagInstanceManager()

    class Meta:
        ordering = ('-when_added',)

    def __unicode__(self):
        """
        Show the flagged object in the unicode string
        """
        app_label, model = get_content_type_tuple(
                self.flagged_content.content_type_id)
        return u'flag on %s.%s #%s by user #%s' % (
                app_label, model, self.flagged_content.object_id, self.user_id)

    def content_settings(self, name):
        """
        Return the settings `name` for the object linked to the flagged_content
        """
        return self.flagged_content.content_settings(name)

    def save(self, *args, **kwargs):
        """
        Save the flag and, if it's a new one, tell it to the flagged_content.
        Also check if set a comment is allowed
        If a `send_signal` is passed, we pass it to the `flag_added` method
        of the flagged_content to tell him to send the signal (default False)
        Idem with `send_mails`, to send emails if settings allow it.
        """
        is_new = not bool(self.id)
        send_signal = kwargs.pop('send_signal', False)
        send_mails = kwargs.pop('send_mails', False)

        # check if the user can flag this object
        if is_new and self.status == 1:
            self.flagged_content.assert_can_be_flagged_by_user(self.user)

        # check comment
        if is_new:
            allow_comments = self.content_settings('ALLOW_COMMENTS')
            if allow_comments and not self.comment:
                raise FlagCommentException(_('You must add a comment'))
            if not allow_comments and self.comment:
                raise FlagCommentException(
                        _('You are not allowed to add a comment'))

        super(FlagInstance, self).save(*args, **kwargs)

        # tell the flagged_content that it has a new flag
        if is_new:
            self.flagged_content.flag_added(self, send_signal=send_signal,
                send_mails=send_mails)

    def send_mails(self):
        """
        Send mails to alert of the current flag
        """
        recipients = self.content_settings('SEND_MAILS_TO')
        if not (self.content_settings('SEND_MAILS') and recipients):
            return

        # prepare recipients
        recipient_list = []
        for recipient in recipients:
            if isinstance(recipient, basestring):
                recipient_list.append(recipient)
            else:
                recipient_list.append(recipient[1])

        # subject and body from templates
        app_label = self.flagged_content.content_object._meta.app_label
        model_name = self.flagged_content.content_object._meta.module_name

        context = dict(
            flag=self,
            flagger=self.user,

            app_label=app_label,
            model_name=model_name,
            object=self.flagged_content.content_object,
            count=self.flagged_content.count,

            object_url=self.flagged_content.get_content_object_absolute_url(),
            object_admin_url=self.flagged_content.\
                    get_content_object_admin_url(),

            flagger_url=self.get_flagger_absolute_url(),
            flagger_admin_url=self.get_flagger_admin_url(),

            site=Site.objects.get_current())

        if self.flagged_content.creator:
            context.update(dict(
                creator=self.flagged_content.creator,
                creator_url=self.flagged_content.get_creator_absolute_url(),
                creator_admin_url=self.flagged_content.\
                        get_creator_admin_url()))

        subject = render_to_string([
                'flag/mail_alert_subject_%s_%s.txt' % (app_label, model_name),
                'flag/mail_alert_subject.txt'],
            context).replace("\n", " ").replace("\r", " ")

        message = render_to_string([
                'flag/mail_alert_body_%s_%s.txt' % (app_label, model_name),
                'flag/mail_alert_body.txt'],
            context)

        # really send the mails !
        send_mail(
            subject=subject,
            message=message,
            from_email=self.content_settings('SEND_MAILS_FROM'),
            recipient_list=recipient_list,
            fail_silently=True)

    def get_flagger_admin_url(self):
        """
        Return the admin url for the flagger
        """
        url = None
        try:
            url = urlresolvers.reverse("admin:auth_user_change",
                args=(self.user_id,))
        except urlresolvers.NoReverseMatch:
            pass
        return url

    def get_flagger_absolute_url(self):
        """
        Return the absolute url for the flagger
        """
        url = None
        try:
            url = self.user.get_absolute_url()
        except (AttributeError,  urlresolvers.NoReverseMatch):
            pass
        return url


def add_flag(flagger, content_type, object_id, content_creator, comment,
        status=None, send_signal=True, send_mails=True):
    """
    This function is here for compatibility
    """
    content_object = content_type.get_object_for_this_type(id=object_id)
    return FlagInstance.objects.add(flagger, content_object, content_creator,
        comment, status, send_signal, send_mails)
