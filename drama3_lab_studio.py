"""Fel's Voice Lab's Drama studio uses the normal broker and trusted Cast versions."""
from pathlib import Path
import wave
from drama3_production import Studio


def create(engines, cast):
    def resolve(source_id):
        if not isinstance(source_id,str):raise ValueError('Choose a saved voice reference.')
        parts=source_id.split(':')
        if len(parts)>2:raise ValueError('Invalid saved voice reference.')
        identity=cast.get_generation_identity(parts[0],parts[1] if len(parts)==2 else None)
        if identity['status']!='approved':raise ValueError('Approve the Cast identity before assigning it to a studio cast.')
        reference=identity.get('reference')
        if not reference:raise ValueError('Choose an approved reference voice with an exact transcript.')
        return {'path':str(cast.get_media_path('reference',reference['id'])),'text':reference['transcript'],
                'source_id':identity['id']+':'+reference['id'],'reference_id':reference['id'],
                'label':identity.get('label',source_id),'allowed_use':reference.get('allowed_use','unknown')}
    def sources():
        rows=[]
        for character in cast.list_cast():
            for identity in character['identities']:
                try:ref=resolve(identity['id'])
                except (KeyError,ValueError):continue
                if ref['text'].strip():rows.append({'id':ref['source_id'],'label':character['name']+' / '+identity['label']})
        return rows
    def runner(body,refs,directory,emit,cancel):
        with wave.open(str(directory/'take.wav'),'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(44100)
            def audio(pcm,rate):
                emit(pcm,rate);wav.writeframesraw(pcm)
            return engines.stream_drama3(body,refs,audio,cancel)
    return Studio(Path(cast.root)/'out/drama3/studio',sources,resolve,runner)
