// Voice Lab drama panel: loads the Drama studio module when its panel opens.
let dramaStudioView;
document.getElementById('dramaStudioDetails').addEventListener('toggle',async function(){
  if(this.open&&!dramaStudioView){const {mountDramaStudio}=await import('/drama3-studio.js');dramaStudioView=await mountDramaStudio(document.getElementById('dramaStudioMount'));}
  else if(!this.open)dramaStudioView?.stopListening();
});
