"use strict";
document.getElementById("print").onclick=()=>window.print();
document.getElementById("confirm").onclick=async event=>{
  event.target.disabled=true;
  try {const r=await fetch(`/api/sellers/${encodeURIComponent(document.body.dataset.seller)}/wb/print/${encodeURIComponent(document.body.dataset.job)}/confirm`,{method:"POST",headers:{"Content-Type":"application/json","X-FBE-Flow":"1"},body:"{}"});if(!r.ok)throw new Error("Не удалось сохранить подтверждение");document.getElementById("result").textContent="Печать подтверждена. Можно вернуться к сборке.";}catch(e){document.getElementById("result").textContent=e.message;event.target.disabled=false;}
};
