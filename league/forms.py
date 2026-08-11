"""Forms for the league app.

A ModelForm is the standard Django idiom when a form maps onto a model: it
builds the fields from the model, validates against the model's own rules, and
`form.save()` writes the row. The fields the user must not set (who they are,
what page they were on, whether the commissioner has dealt with it) are simply
left out of `fields`, which is what keeps them unsettable from a POST body.
"""

from django import forms

from .models import DraftPollAlternative, Feedback


class FeedbackForm(forms.ModelForm):
    class Meta:
        model = Feedback
        fields = ['message']
        labels = {
            'message': 'Your note',
        }
        widgets = {
            'message': forms.Textarea(
                attrs={
                    'rows': 6,
                    'placeholder': (
                        'What would you change, add, or fix? The more specific '
                        'the better -- which page, and what you expected.'
                    ),
                }
            ),
        }

    def clean_message(self):
        """Reject a note that is technically non-empty but says nothing.

        `clean_<field>` is the Django hook for validating one field; whatever it
        returns becomes the cleaned value, so the strip is kept as well.
        """
        message = self.cleaned_data['message'].strip()
        if len(message) < 5:
            raise forms.ValidationError('Tell us a little more than that.')
        return message


class DraftPollAlternativeForm(forms.ModelForm):
    """One team's "if none of these work, when does?" note.

    `poll` and `team` are not fields: the view sets them from the poll it
    already loaded and from the session, so a forged POST body cannot file a
    note under somebody else's name.
    """

    class Meta:
        model = DraftPollAlternative
        fields = ['text']
        labels = {
            'text': 'If none of these work, when does?',
        }
        widgets = {
            'text': forms.Textarea(
                attrs={
                    'rows': 3,
                    'placeholder': (
                        'e.g. any weeknight after 8, or the Saturday before -- '
                        'anything that helps find a time.'
                    ),
                }
            ),
        }

    def clean_text(self):
        text = self.cleaned_data['text'].strip()
        if len(text) < 5:
            raise forms.ValidationError('Tell us a little more than that.')
        return text
