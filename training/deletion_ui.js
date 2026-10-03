(function(root,factory){
 const value=factory();
 if(typeof module==='object'&&module.exports)module.exports=value;
 else root.DeletionUI=value;
})(typeof globalThis!=='undefined'?globalThis:this,function(){
 function shouldPoll(value){return !!value&&value.logical_status==='deleted'&&value.cleanup_status==='pending'}
 function nextRunId(runs,deletedId,previousIndex){
  const remaining=(runs||[]).filter(run=>run.id!==deletedId);
  if(!remaining.length)return null;
  return remaining[Math.min(Math.max(0,previousIndex||0),remaining.length-1)].id;
 }
 function statusText(value){
  if(!value)return '';
  if(value.cleanup_status==='manual_intervention_required')return value.logical_status==='deleted'?'模型已从列表移除，但后台清理需要人工检查。':'删除未提交，需要人工检查；模型仍然保留。';
  if(value.cleanup_status==='cleaned')return '模型和待删除文件已清理完成。';
  return value.logical_status==='deleted'?'模型已从列表移除，大型文件正在后台清理。':'正在准备安全删除，模型尚未移除。';
 }
 function recoverForRun(deletions,runId){return (deletions||[]).find(value=>value.run_id===runId)||null}
 return {shouldPoll,nextRunId,statusText,recoverForRun};
});
