"""Fel's Voice Lab adapter with the same trusted reference containment as other engines."""
from drama3_runtime import SpeechRuntime, controls
from reference_paths import resolve_reference_path
from common import runtime_setting


class Drama3Worker:
    def __init__(self, device):
        self.runtime = SpeechRuntime(device='cpu' if device=='cpu' else runtime_setting('drama_design_device','cpu'))

    def load(self):
        return self.runtime.load()

    def prepare_reference(self, params):
        reference = resolve_reference_path('drama-3',params.get('path'))
        self.runtime.load()
        voice, _ = self.runtime.prepare_voice([{'path':str(reference),'text':params.get('reference_text','')}])
        return {'prepared':True,'voice':voice,'path':str(reference)}

    def generate(self, params, on_audio=None):
        references = params.get('references')
        if references:
            references=[{**ref,'path':str(resolve_reference_path('drama-3',ref['path']))} for ref in references]
        if params.get('mode','design') == 'clone':
            references = [{'path':str(resolve_reference_path('drama-3',params.get('reference_path'))),
                           'text':params.get('reference_text','')}]
        return self.runtime.generate(params['text'],description=params.get('voice_description',''),
            direction=params.get('direction',''),references=references,
            on_audio=on_audio,cpu_only=params.get('cpu_only',False),
            settings={key:params[key] for key in controls() if key in params})

    def unload(self):
        self.runtime.close()
        return {'model_loaded':False}
