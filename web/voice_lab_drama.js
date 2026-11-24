// Voice Lab drama panel: loads the Drama studio module when its panel opens.
let dramaStudioView;
const dramaStudioDetails=/** @type {HTMLDetailsElement} */ (document.getElementById('dramaStudioDetails'));
dramaStudioDetails.addEventListener('toggle',async function(){
  if(dramaStudioDetails.open&&!dramaStudioView){const {mountDramaStudio}=await import('/drama3-studio.js');dramaStudioView=await mountDramaStudio(document.getElementById('dramaStudioMount'));}
  else if(!dramaStudioDetails.open)dramaStudioView?.stopListening();
});
